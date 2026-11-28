"""Row 581 (t_ae574709, N2): a bare `tasks.current_run_id` pointer never marks a card owned.
`lease.start_run` always inserts status='running', so every real restart (checkpoint,
invalid_doc) is already owned through the live `task_runs` query; a pointer to an ended or
missing run is a leak, and used to read as owned forever.

Z2, two oracles: `board_items`' own owner, and `summarize` (the UNOWNED count the Stop hook
uses), which shares no code with the owner judgement.
"""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import db, lease, paths, unblocked


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


def _board(pointer, status="ready"):
    """A card (default `ready`); `pointer` is "ended", "running", "missing" or None.

    Real `lease` writes.
    """
    home = Path(tempfile.mkdtemp())
    p = home / "boards" / "capsule-corp" / "kanban.db"
    p.parent.mkdir(parents=True)
    _create_board(p)
    conn = db.connect(p)
    tid = db.new_task(conn, title="mid-workflow", status=status)
    if pointer == "missing":
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (999, tid))
    elif pointer:
        run_pk = lease.start_run(conn, tid, role="implementer", claim_lock="h:r", expires=5000,
                                 run_id="r1", now=1000)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_pk, tid))
        if pointer == "ended":
            lease.end_run(conn, run_pk, status="crashed", outcome="crashed", now=1500)
            conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_pk, tid))    # the leak
    conn.commit()
    conn.close()
    return home


def _items(home, dispatcher=None):
    caps = None if dispatcher is None else {
        "max_running": 1, "epic_branches": [], "assignees": {}, "dispatcher": dispatcher}
    return unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                 run_status_by_commit={}, caps=caps)


class LeakedPointer(unittest.TestCase):
    def test_a_ready_card_pointing_at_an_ended_run_is_unowned(self):
        items = _items(_board("ended"))
        self.assertEqual(items[0].owner, "")
        self.assertEqual(unblocked.summarize(items), (1, 1))

    def test_a_ready_card_pointing_at_a_missing_run_is_unowned(self):
        self.assertEqual(_items(_board("missing"))[0].owner, "")

    def test_a_checkpoint_restart_with_a_running_run_row_is_still_owned(self):
        items = _items(_board("running"))
        self.assertEqual(items[0].owner, "live-run")
        self.assertEqual(unblocked.summarize(items), (1, 0))


class IntentExamples(unittest.TestCase):
    """Z1: every `contracts/intent/unblocked_ownership.jsonl` example through `board_items`."""

    def test_every_example_matches_its_oracle(self):
        lines = (paths.INTENT / "unblocked_ownership.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            row = json.loads(line)
            kind = "running" if row["given"]["document"]["current_run_id"] == "running-run" else "ended"
            doc = row["given"]["document"]
            home = _board(kind, doc.get("status", "ready"))
            owner = _items(home, doc.get("dispatcher"))[0].owner
            self.assertEqual(owner or "<empty>", row["expect"]["owner"], row["id"])


if __name__ == "__main__":
    unittest.main()
