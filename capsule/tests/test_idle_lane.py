"""Z5 idle-lane detector (2026-10-26 incident): 0 running and a held ready card for 15 minutes or
more is an item in `capsule unblocked` and a problem in `capsule status` ATTENTION. Oracle 1: a
fixture board whose holder is `needs_input` for 16 minutes yields one item naming the holder.
Oracle 2: a running card, no ready card, a young idle, or no holder yields none."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, dispatch, idle_lane, paths, row_schemas, statusview, tick_guard, unblocked
from capsule.tests.private_names import retired_names

BOARD, EPIC, NOW = "capsule-corp", "epic/x", 1_800_000_000
MIN = 60


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


def _configure(home):
    (home / "config.json").write_text(json.dumps({
        "board": BOARD, "caps": {"max_running": 2}, "epics": [EPIC, "epic/y"],
        "assignees": {"impl-1": "implementer"}, "routing": {"implementer": {}},
        "unblocked": {"boards": [BOARD], "repos": []}}))
    tick_guard.lock_path(home, BOARD).write_text("")


def fixture(*, running=False, ready=True, holder="blocked", kind="needs_input", idle_min=16,
            hold_reason=True, live_review=False, deferred=False, paused=False, parent=False,
            ready_event_min=None):
    """A board: a holder card (`blocked`/`needs_input` with a run that ended `idle_min` ago, or a
    realistic `review` with `kind=None`), a ready card on the same branch, and the options that
    make the lane busy (`running`, `live_review`, `paused`) or normal flow (`deferred`, `parent`).
    `ready_event_min`: the ready card's own newest event, that many minutes ago."""
    home = Path(tempfile.mkdtemp())
    p = paths.board_db(BOARD, home)
    p.parent.mkdir(parents=True)
    _create_board(p)
    conn = db.connect(p)
    ended = NOW - idle_min * MIN
    holder_id = db.new_task(conn, title="holder card", status=holder, branch=EPIC, now=ended - 3600)
    conn.execute("UPDATE tasks SET block_kind=? WHERE id=?", (kind, holder_id))
    if hold_reason or live_review:
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at)"
                     " VALUES (?, 'implementer', 'done', ?, ?)", (holder_id, ended - 600, ended))
    if live_review:       # an open reviewer run with no recorded pid reads as alive
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at)"
                     " VALUES (?, 'reviewer', 'running', ?)", (holder_id, ended))
    if deferred:
        db.event(conn, holder_id, "review_deferred",
                 {"roles": ["reviewer"], "live": 2, "cap": 2, "reason": "review_cap"}, now=ended)
    ready_id = None
    if ready:
        ready_id = db.new_task(conn, title="waiting card", status="ready", branch=EPIC,
                               assignee="impl-1", now=ended - 1800,
                               parents=[holder_id] if parent else ())
        if ready_event_min is not None:
            db.event(conn, ready_id, "promoted", now=NOW - ready_event_min * MIN)
    if running:
        other = db.new_task(conn, title="running card", status="running", branch="epic/y",
                            now=ended)
        conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (NOW + 600, other))
    if paused:
        db.guard_set(conn, "pause:account:x", {"until": NOW + 3600}, now=NOW)
    conn.close()
    _configure(home)
    return home, holder_id, ready_id


def idle_items(home):
    items = unblocked.detect(capsule_home=home, now=NOW,
                             dispatcher_check=lambda b, h: "live")
    return [i for i in items if "idle lane" in i.item]


class IdleLaneItem(unittest.TestCase):
    def test_needs_input_holder_for_16_minutes_names_holder_hold_and_oldest_ready(self):
        home, holder, ready = fixture()
        (it,) = idle_items(home)
        self.assertEqual((it.state, it.owner, it.source), ("unblocked", "", f"board:{BOARD}"))
        self.assertIn(holder, it.item)
        self.assertIn(ready, it.item)
        self.assertIn("needs_input", it.item)
        self.assertEqual(it.blocker, f"idle-lane: branch held by {holder} (blocked: needs_input)")
        self.assertGreaterEqual(it.age_s, 16 * MIN)
        self.assertEqual(unblocked.summarize([it]), (1, 1))

    def test_item_text_is_stable_between_ticks(self):
        """The Stop hook re-blocks when an (item, blocker) pair changes: no clock in the text."""
        home, _, _ = fixture()
        items = idle_items(home)
        self.assertTrue(items, "idle_items(home) found no idle lane to compare")
        first = items[0]
        later = unblocked.detect(capsule_home=home, now=NOW + 5 * MIN,
                                 dispatcher_check=lambda b, h: "live")
        again = next(i for i in later if "idle lane" in i.item)
        self.assertEqual((first.item, first.blocker), (again.item, again.blocker))

    def test_review_holder_reads_branch_busy(self):
        home, holder, _ = fixture(holder="review", kind=None)    # review, nothing live, no deferral
        (it,) = idle_items(home)
        self.assertEqual(it.blocker, "idle-lane: branch-busy")
        self.assertIn(holder, it.item)

    def test_report_json_is_a_valid_item(self):
        home, _, _ = fixture()
        doc = json.loads(unblocked.render_json(idle_items(home)))
        self.assertTrue(doc["items"], "render_json produced no items to validate")
        self.assertEqual(row_schemas.problems("unblocked_item.v1", doc["items"][0]), [])

    def test_detect_no_longer_takes_the_retired_home(self):
        """D244: the old board fallback is gone, so the kwarg is too."""
        for name in retired_names():
            with self.assertRaises(TypeError):
                gone = {f"{name}_home": Path(f"/no-{name}")}
                unblocked.detect(capsule_home=Path(tempfile.mkdtemp()), **gone)


class NoIdleLaneItem(unittest.TestCase):
    def test_a_running_card_means_the_lane_is_not_idle(self):
        self.assertEqual(idle_items(fixture(running=True)[0]), [])

    def test_no_ready_card_yields_nothing(self):
        self.assertEqual(idle_items(fixture(ready=False)[0]), [])

    def test_idle_under_15_minutes_yields_nothing(self):
        self.assertEqual(idle_items(fixture(idle_min=14)[0]), [])

    def test_a_live_review_means_the_lane_is_not_idle(self):
        self.assertEqual(idle_items(fixture(holder="review", kind=None, live_review=True)[0]), [])

    def test_a_review_holder_deferred_for_a_seat_is_normal_flow(self):
        self.assertEqual(idle_items(fixture(holder="review", kind=None, deferred=True)[0]), [])

    def test_a_paused_board_is_a_deliberate_stop(self):
        self.assertEqual(idle_items(fixture(paused=True)[0]), [])

    def test_a_ready_card_with_an_open_parent_is_not_dispatchable(self):
        self.assertEqual(idle_items(fixture(parent=True)[0]), [])

    def test_a_ready_card_that_just_became_ready_is_not_idle_yet(self):
        """Its own newest event (5m ago) is newer than the last run end (16m ago)."""
        self.assertEqual(idle_items(fixture(ready_event_min=5)[0]), [])

    def test_ready_card_nothing_holds_yields_nothing(self):
        home, holder, _ = fixture(hold_reason=False)    # blocked, no run newer than a done card
        self.assertEqual(idle_items(home), [])


class StatusAttention(unittest.TestCase):
    def problems(self, home):
        snap = statusview.snapshot(home, now=NOW, boards=[BOARD], events_path=home / "none.jsonl")
        return [p["what"] for p in snap["attention"]["open"] if "nothing is running" in p["what"]]

    def test_idle_lane_is_a_problem_naming_holder_and_oldest_ready(self):
        (what,) = self.problems(fixture()[0])
        self.assertIn("holder card", what)
        self.assertIn("waiting card", what)
        self.assertIn("shared workspace", what)
        self.assertNotIn("clone-dirty", what)

    def test_review_holder_reads_in_review_on_the_same_branch(self):
        (what,) = self.problems(fixture(holder="review", kind=None)[0])
        self.assertIn("in review on the same branch", what)
        self.assertNotIn("branch-busy", what)

    def test_running_or_no_ready_is_quiet(self):
        self.assertEqual(self.problems(fixture(running=True)[0]), [])
        self.assertEqual(self.problems(fixture(ready=False)[0]), [])


class WorkRootPassedToCloneDirty(unittest.TestCase):
    """Each call site hands the board's clone root to `clone_dirty`; dropping it makes every clone
    read as not clean (the `ready` twin is in test_dispatch)."""

    def conn_card(self):
        home, _, ready = fixture()
        conn = db.connect(paths.board_db(BOARD, home))
        self.addCleanup(conn.close)
        return home, conn, conn.execute("SELECT * FROM tasks WHERE id=?", (ready,)).fetchone()

    def test_find_passes_its_work_root(self):
        _, conn, _ = self.conn_card()
        with mock.patch.object(dispatch, "clone_dirty", return_value=None) as cd:
            idle_lane.find(conn, NOW, "ROOT")
        self.assertTrue(cd.called)
        self.assertTrue(all(c.args[3] == "ROOT" for c in cd.call_args_list))

    def test_hold_reason_passes_its_work_root(self):
        _, conn, card = self.conn_card()
        with mock.patch.object(dispatch, "clone_dirty", return_value=None) as cd:
            unblocked.dispatcher_hold_reason(conn, card, set(), work_root="ROOT")
        self.assertEqual(cd.call_args.args[3], "ROOT")

    def test_ready_card_gate_passes_the_boards_work_root(self):
        home, conn, card = self.conn_card()
        caps = {"epic_branches": [EPIC], "max_running": 2, "assignees": {}}
        with mock.patch.object(dispatch, "clone_dirty", return_value=None) as cd, \
                mock.patch.object(dispatch, "resolve_assignee", return_value="x"):
            unblocked._ready_card_gate(caps, card, board=BOARD, conn=conn, paused=False,
                                       running_count=0, busy_branches=set(), home=home)
        self.assertEqual(cd.call_args.args[3], dispatch.root(BOARD, home))

    def test_idle_lane_items_pass_the_boards_work_root(self):
        home, _, _ = fixture()
        with mock.patch.object(idle_lane, "find", return_value=None) as find:
            unblocked._idle_lane_items(BOARD, capsule_home=home, now=NOW)
        self.assertEqual(find.call_args.args[2], dispatch.root(BOARD, home))

    def test_idle_lane_problems_pass_the_boards_work_root(self):
        home, conn, _ = self.conn_card()
        with mock.patch.object(idle_lane, "find", return_value=None) as find:
            statusview._idle_lane_problems(conn, BOARD, NOW, home)
        self.assertEqual(find.call_args.args[2], dispatch.root(BOARD, home))


if __name__ == "__main__":
    unittest.main()
