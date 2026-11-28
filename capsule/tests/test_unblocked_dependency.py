# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   RenderTable.test_dependency_blocked_items_get_their_own_heading
"""`capsule unblocked`'s fourth blocker type, `dependency` (row 482, t_92b31199): a card
whose parent card (`task_links`, `db.open_parent`'s own table) is not `done`/`archived`
is blocked, `dependency: <parent id>` - never derived from card text. Z2: two oracles per
case - `dependency_blocked`'s own pure sqlite read, and `board_items`'s end-to-end fixture
(state AND the blocker text naming the parent), which share no code path with each other.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from capsule import db, paths, unblocked


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


def _board():
    p = Path(tempfile.mkdtemp()) / "k.db"
    _create_board(p)
    return db.connect(p)


class DependencyBlockedFn(unittest.TestCase):
    """The pure sqlite oracle `board_items` applies - the second oracle is `BoardItems`
    below, an end-to-end fixture that shares no code path with this."""

    def test_unfinished_parent_blocks_with_its_own_id(self):
        conn = _board()
        parent = db.new_task(conn, title="parent", status="ready")
        child = db.new_task(conn, title="child", status="ready", parents=(parent,))
        blocked, blocker = unblocked.dependency_blocked(conn, child)
        conn.close()
        self.assertTrue(blocked)
        self.assertEqual(blocker, f"dependency: {parent}")

    def test_done_parent_does_not_block(self):
        conn = _board()
        parent = db.new_task(conn, title="parent", status="done")
        child = db.new_task(conn, title="child", status="ready", parents=(parent,))
        blocked, blocker = unblocked.dependency_blocked(conn, child)
        conn.close()
        self.assertFalse(blocked)
        self.assertEqual(blocker, "")

    def test_archived_parent_does_not_block(self):
        conn = _board()
        parent = db.new_task(conn, title="parent", status="archived")
        child = db.new_task(conn, title="child", status="ready", parents=(parent,))
        blocked, _ = unblocked.dependency_blocked(conn, child)
        conn.close()
        self.assertFalse(blocked)

    def test_no_parent_links_is_unblocked(self):
        conn = _board()
        child = db.new_task(conn, title="child", status="ready")
        blocked, blocker = unblocked.dependency_blocked(conn, child)
        conn.close()
        self.assertFalse(blocked)
        self.assertEqual(blocker, "")

    def test_missing_task_links_table_never_raises(self):
        """A board schema older than `task_links` itself (`_fallback_cards`'s own case) is
        never blocked by this - the table simply isn't there to ask."""
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        raw.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
        blocked, blocker = unblocked.dependency_blocked(raw, "t1")
        raw.close()
        self.assertFalse(blocked)
        self.assertEqual(blocker, "")


class BoardItems(unittest.TestCase):
    """End-to-end through `board_items`, same read-only sqlite fixture shape
    `test_unblocked.py`'s own `BoardAdapter` uses."""

    def _items(self, parent_status, waits_body="", status="ready"):
        home = Path(tempfile.mkdtemp()) / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        parent = db.new_task(conn, title="epic parent", status=parent_status)
        child = db.new_task(conn, title="waiting on parent", status=status,
                             parents=(parent,), body=waits_body)
        conn.close()
        items = unblocked.board_items(
            "capsule-corp", capsule_home=home, now=2000,
            run_status_by_commit={"abc1234": ["running"]} if waits_body else {})
        return parent, next(i for i in items if child in i.item)

    def test_card_with_unfinished_parent_is_blocked_dependency(self):
        """Row 482 (reproduce): before this fix, a card whose parent card is not done fell
        straight through `board_items` to unblocked/unowned - `task_links` was never
        consulted at all."""
        parent, item = self._items("ready")
        # Two oracles (Z2): the item's own `state`, and the blocker text naming the parent.
        self.assertEqual(item.state, "blocked")
        self.assertEqual(item.blocker, f"dependency: {parent}")

    def test_finished_parent_removes_the_dependency_blocker(self):
        _, item = self._items("done")
        self.assertEqual(item.state, "unblocked")
        self.assertEqual(item.blocker, "")

    def test_card_body_waits_line_still_wins_over_an_unfinished_parent(self):
        """`waits:` text on the card itself is checked first (`evaluate_blocker`) - a
        dependency blocker never overrides an already-blocked card, only fills in once
        `waits:` itself found nothing."""
        _, item = self._items("ready", waits_body="waits: report-commit abc1234\n")
        self.assertEqual(item.state, "blocked")
        self.assertEqual(item.blocker, "waits: report-commit abc1234")

    def test_review_card_with_an_open_parent_stays_unblocked(self):
        """Review round 1, I1: only `lease.claim` enforces parents, and it only ever claims
        `ready` cards - a `review` card with an open parent is never actually held back by
        anything, so reporting it `blocked` would be a comforting mislabel (row 483)."""
        _, item = self._items("running", status="review")
        self.assertEqual(item.state, "unblocked")
        self.assertEqual(item.blocker, "")


class IntentExamples(unittest.TestCase):
    """Z1: every `contracts/intent/unblocked_dependency.jsonl` example runs through the real
    `board_items` oracle its own document names - two oracles (Z2): `state` against
    `expect.blocked`, and the blocker text against `expect.blocker` when given."""

    def test_every_example_matches_its_oracle(self):
        lines = (paths.INTENT / "unblocked_dependency.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        board = BoardItems()
        for line in lines:
            row = json.loads(line)
            doc = row["given"]["document"]
            waits = "" if doc["waits"] == "<empty>" else f"waits: {doc['waits']}\n"
            _, item = board._items(doc["parent_status"], waits_body=waits, status=doc["status"])
            self.assertEqual(item.state == "blocked", row["expect"]["blocked"], row["id"])
            if "blocker" in row["expect"]:
                self.assertTrue(item.blocker.startswith(row["expect"]["blocker"]), row["id"])


class RenderTable(unittest.TestCase):
    def test_dependency_blocked_items_get_their_own_heading(self):
        items = [unblocked.Item("sample-app:t_5 card five", "board:sample-app", "blocked",
                                 blocker="dependency: t_parent1")]
        out = unblocked.render_table(items)
        self.assertIn("-- dependency", out)
        self.assertIn("card five", out.split("-- dependency")[1])
        # still shown (and counted as blocked, not unowned) in the main table too
        self.assertIn("card five", out.split("-- dependency")[0])


if __name__ == "__main__":
    unittest.main()
