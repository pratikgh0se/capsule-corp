"""Row 569 item 4: `contracts/schemas/rows/unblocked_item.v1.json`, the shape of one item in the
unblocked detector's `--json` report (`unblocked.Item.as_dict`), which gates dispatch, the Stop hook
and the dead-man metric. Oracle 1: items from every real adapter (board cards incl. blocked and
the legacy-board notice, a human file, a git branch, a run layer) round-tripped through the real
`render_json`. Oracle 2: hand-written bad items are rejected, and the hook's own read of the report
(`unblocked_stop`) agrees on which items count.

NOT wired into `intent.lint` (see the report: `contracts/intent/unblocked.jsonl` documents are
classification scenarios, not `Item` rows)."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from capsule import db, human, intent, row_schemas, unblocked

GOOD = {"item": "capsule-corp:t_0123abcd do it", "source": "board:capsule-corp", "state": "unblocked",
        "blocker": "", "owner": "", "age_s": 12}


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


class RealProducer(unittest.TestCase):
    def report(self):
        home = Path(tempfile.mkdtemp())
        board = home / "boards" / "capsule-corp" / "kanban.db"
        board.parent.mkdir(parents=True)
        _create_board(board)
        conn = db.connect(board)
        db.new_task(conn, title="ready and unowned", status="ready", branch="epic/x")
        db.new_task(conn, title="waits on a human", status="ready", branch="epic/x",
                    body="waits: owner")
        conn.close()
        human.write(home, "capsule-corp", "t_0123abcd", "unblock", "go", now=5)
        repo = Path(tempfile.mkdtemp())
        git(repo, "init", "-q", "-b", "main")
        (repo / "f.txt").write_text("1")
        git(repo, "add", "f.txt")
        git(repo, "commit", "-q", "-m", "init")
        git(repo, "checkout", "-q", "-b", "fix/thing")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        layer = Path(tempfile.mkdtemp()) / "progress.json"
        layer.write_text(json.dumps({"commit": "abc1234", "status": "running", "layers": {"l1": {"status": "done"}}}))
        now = time.time()
        items = (unblocked.board_items("capsule-corp", capsule_home=home, now=now,
                                       run_status_by_commit={},
                                       caps={"max_running": 5, "epic_branches": ["epic/x"]})
                 + unblocked.human_items("capsule-corp", capsule_home=home, now=now)
                 + unblocked.branch_items(repo, review_notes=None, now=now, run_status_by_commit={})
                 + unblocked.run_layer_items([str(layer)], now=now))
        notice = unblocked.Item("board:x: checks skipped", "board:x", "notice")
        return json.loads(unblocked.render_json(items + [notice]))

    def test_every_item_of_every_real_adapter_validates(self):
        doc = self.report()
        sources = {i["source"].split(":")[0] for i in doc["items"]}
        self.assertEqual(sources, {"board", "human", "branch", "run-layer"})
        self.assertIn("blocked", {i["state"] for i in doc["items"]})
        for item in doc["items"]:
            with self.subTest(item=item["item"][:40], source=item["source"]):
                self.assertEqual(row_schemas.problems("unblocked_item.v1", item), [])

    def test_the_reports_counts_match_the_items_the_schema_calls_unblocked_and_unowned(self):
        doc = self.report()
        unblocked_items = [i for i in doc["items"] if i["state"] == "unblocked"]
        self.assertEqual(doc["unblocked"], len(unblocked_items))
        self.assertEqual(doc["unowned"], len([i for i in unblocked_items if not i["owner"]]))


class Rejections(unittest.TestCase):
    def test_a_good_item_is_valid(self):
        self.assertEqual(row_schemas.problems("unblocked_item.v1", GOOD), [])
        self.assertEqual(row_schemas.problems("unblocked_item.v1", dict(GOOD, age_s=None)), [])

    def test_hand_written_bad_items_are_rejected(self):
        bad = {
            "state outside the four": dict(GOOD, state="idle"),
            "state misspelt": dict(GOOD, state="unblock"),
            "missing owner": {k: v for k, v in GOOD.items() if k != "owner"},
            "owner is null": dict(GOOD, owner=None),
            "negative age": dict(GOOD, age_s=-1),
            "unknown source": dict(GOOD, source="queue"),
            "empty item": dict(GOOD, item=""),
            "extra key": dict(GOOD, blocked=False),
        }
        for label, item in bad.items():
            with self.subTest(label):
                self.assertTrue(row_schemas.problems("unblocked_item.v1", item), label)

    def test_the_schema_state_enum_is_the_states_the_stop_hook_and_summarize_know(self):
        schema = row_schemas.load("unblocked_item.v1")
        self.assertEqual(set(schema["properties"]["state"]["enum"]), {"unblocked", "blocked", "unclassified", "notice"})


class IntentExamples(unittest.TestCase):
    """Z1: `contracts/intent/unblocked_item_schema.jsonl`, each example run through the real schema."""

    def test_every_example_matches_what_the_schema_does(self):
        examples = intent.load("unblocked_item_schema")
        self.assertTrue(1 <= len(examples) <= 3)
        for ex in examples:
            row = ex["given"]["document"]["row"]
            self.assertEqual(row_schemas.problems("unblocked_item.v1", row) == [], ex["expect"]["valid"], ex["id"])


class SpecificViolations(unittest.TestCase):
    def test_the_message_names_the_field_and_the_rule(self):
        self.assertEqual(row_schemas.problems("unblocked_item.v1", dict(GOOD, state="idle")),
                         ["#/state: must be one of ['unblocked', 'blocked', 'unclassified', 'notice']"])
        self.assertEqual(row_schemas.problems("unblocked_item.v1", dict(GOOD, extra=1)),
                         ["#: unexpected field 'extra'"])
