# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   OneStore.test_board_named_by_the_legacy_top_level_config_is_read,
#   OneStore.test_board_without_a_capsule_config_is_not_read
"""C25 (t_9daf4abc): a board with a Capsule config is read from its Capsule db only.

Z1 intent example (named source: card t_9daf4abc body): a legacy db with no marker used to win;
with `boards/<board>/config.json` present it is never opened, even when the Capsule db is absent.
Z2 second oracle: the item text proves which db was read.
"""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import db, paths, unblocked


def _make(path, title):
    path.parent.mkdir(parents=True, exist_ok=True)
    db.create_board(path)
    conn = db.connect(path)
    db.new_task(conn, title=title, status="ready")
    conn.close()


def _is_c25(doc):
    return "capsule_config_json" in doc or "legacy_top_level_board" in doc


def c25_example_holds(row=None):
    """The intent row's oracle: `_board_db_candidates` for a temp board home, really called.
    Given keys: `capsule_config_json` (a `boards/b/config.json` exists) or
    `legacy_top_level_board` (the top-level config names board `b`); expect `db_read` is
    `capsule` or `none`. No argument: every shipped row with those keys must hold, and all
    three cases (config.json, legacy, neither) must be present, so a row going missing fails."""
    if row is None:
        rows = [json.loads(r) for r in (paths.INTENT / "unblocked.jsonl").read_text().splitlines()]
        rows = [r for r in rows if _is_c25(r["given"]["document"])]
        cases = {(bool(r["given"]["document"].get("capsule_config_json")),
                  bool(r["given"]["document"].get("legacy_top_level_board"))) for r in rows}
        return cases == {(True, False), (False, True), (False, False)} \
            and all(c25_example_holds(r) for r in rows)
    doc, expect = row["given"]["document"], row["expect"]
    home = Path(tempfile.mkdtemp())
    if doc.get("capsule_config_json"):
        paths.config_path(home, "b").parent.mkdir(parents=True)
        paths.config_path(home, "b").write_text("{}")
    if doc.get("legacy_top_level_board"):
        (home / "config.json").write_text(json.dumps({"board": "b", "caps": {"max_running": 1}}))
    got = unblocked._board_db_candidates("b", home)
    read = "capsule" if got == [paths.board_db("b", home)] else "none"
    return read == expect["db_read"]


class OneStore(unittest.TestCase):
    def _items(self, home, board="capsule-corp"):
        return unblocked.board_items(board, capsule_home=home, now=1,
                                     run_status_by_commit={})

    def test_configured_board_reads_only_its_capsule_db(self):
        home = Path(tempfile.mkdtemp())
        (home / "boards" / "capsule-corp").mkdir(parents=True)
        (home / "boards" / "capsule-corp" / "config.json").write_text("{}")
        self.assertEqual(self._items(home), [])
        _make(home / "boards" / "capsule-corp" / "kanban.db", "from capsule")
        items = self._items(home)
        self.assertEqual(len(items), 1)
        self.assertIn("from capsule", items[0].item)

    def test_board_named_by_the_legacy_top_level_config_is_read(self):
        home = Path(tempfile.mkdtemp())
        cfg = {"board": "sample-app", "caps": {"max_running": 1}}
        (home / "config.json").write_text(json.dumps(cfg))
        _make(home / "boards" / "sample-app" / "kanban.db", "from capsule")
        items = self._items(home, "sample-app")
        self.assertEqual(len(items), 1)
        self.assertIn("from capsule", items[0].item)

    def test_board_without_a_capsule_config_is_not_read(self):
        """D244: the old legacy boards are dropped, so no legacy db is read and a Capsule db
        with no config is not read either."""
        home = Path(tempfile.mkdtemp())
        _make(home / "boards" / "default" / "kanban.db", "from capsule")
        self.assertEqual(unblocked._board_db_candidates("default", home), [])
        self.assertEqual(self._items(home, "default"), [])


if __name__ == "__main__":
    unittest.main()
