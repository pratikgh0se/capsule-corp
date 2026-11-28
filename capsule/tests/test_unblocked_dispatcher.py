# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   Dispatcher.test_detect_no_longer_takes_the_old_home_kwarg
"""Factory autonomy Task 10 (spec 4.5): a board's ready cards count as capacity/no-epic only while a
tick really serves it; with no dispatcher they are unowned, and the hook says so."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from capsule import db, paths, tick_guard, unblocked

EPIC = "epic/x"
ASSIGNEE = "impl-1"      # fix round 1, F2/row 492: a routable assignee, so `unassigned` never
ROUTING = {"assignees": {ASSIGNEE: "implementer"}, "routing": {"implementer": {}}}  # shadows capacity/no-epic


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


def ps(*rows):
    """A fake `ps -Awwo pid,ppid,args` listing `rows` of (pid, argv)."""
    out = "  PID  PPID ARGS\n" + "".join(f"{pid} 1 {argv}\n" for pid, argv in rows)
    return lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr="")


NO_TICKS = ps()


def home_with(board, cfg_where, ran_tick=True, routing=True):
    """`routing=False` (round 2 re-check R1-F1): the `waiting` card gets no assignee and the
    config carries no `assignees`/`routing` block at all - the shape main's own ruling
    (`test_unblocked.py:20-22`) must still block as `unassigned`, not read as unrouted-and-moot."""
    home = Path(tempfile.mkdtemp())
    p = paths.board_db(board, home)
    p.parent.mkdir(parents=True)
    _create_board(p)
    conn = db.connect(p)
    db.new_task(conn, title="running", status="running", branch=EPIC)
    db.new_task(conn, title="waiting", status="ready", branch=EPIC, assignee=ASSIGNEE if routing else None)
    conn.commit()
    conn.close()
    cfg = {"board": board, "caps": {"max_running": 1}, "epics": [EPIC], **(ROUTING if routing else {})}
    if cfg_where == "legacy":
        (home / "config.json").write_text(json.dumps(dict(cfg, unblocked={"boards": [board], "repos": []})))
    else:
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": [board], "repos": []}}))
        paths.config_path(home, board).write_text(json.dumps(cfg))
    if ran_tick:                                      # a guarded tick has held this board's lock before
        tick_guard.lock_path(home, board).write_text("")
    return home


class Dispatcher(unittest.TestCase):
    def item(self, home, board, state):
        items = unblocked.detect(capsule_home=home, now=2000,
                                 dispatcher_check=lambda b, h: state)
        return next(i for i in items if i.source == f"board:{board}" and "waiting" in i.item)

    def test_per_board_config_is_read(self):
        home = home_with("capsule-corp", "per-board")
        caps = unblocked.load_caps_all(home)
        self.assertEqual(caps["capsule-corp"]["max_running"], 1)
        self.assertTrue(self.item(home, "capsule-corp", "live").blocker.startswith("capacity:"))

    def test_no_dispatcher_is_unowned(self):
        for where in ("legacy", "per-board"):
            with self.subTest(where=where):
                home = home_with("capsule-corp", where)
                it = self.item(home, "capsule-corp", "none")
                self.assertEqual((it.state, it.owner, it.blocker), ("unblocked", "", "no-dispatcher"))
                self.assertEqual(unblocked.summarize([it]), (1, 1))

    def test_assigned_ready_card_is_not_dropped_with_no_dispatcher(self):
        """Fix round 1, F1/spec 4.5: `board_items`' own "assigned, about to dispatch" drop must
        never swallow the `no-dispatcher` verdict - an assigned card is exactly as unowned as an
        unassigned one once no tick serves the board at all (row 496(9))."""
        home = Path(tempfile.mkdtemp())
        board = "capsule-corp"
        p = paths.board_db(board, home)
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="assigned", status="ready", branch=EPIC, assignee="impl-1")
        conn.commit(); conn.close()
        (home / "config.json").write_text(json.dumps(
            {"board": board, "caps": {"max_running": 1}, "epics": [EPIC],
             "unblocked": {"boards": [board], "repos": []}}))
        tick_guard.lock_path(home, board).write_text("")
        items = unblocked.detect(capsule_home=home, now=2000,
                                  dispatcher_check=lambda b, h: "none")
        it = next(i for i in items if i.source == f"board:{board}")
        self.assertEqual((it.state, it.owner, it.blocker), ("unblocked", "", "no-dispatcher"))
        self.assertEqual(unblocked.summarize([it]), (1, 1))

    def test_a_board_without_a_lock_is_unowned_after_the_cutover(self):
        """Task 11 removes the `legacy` shortcut (spec 4.5): a board with no `tick-<board>.lock`
        (no guarded tick has ever served it) is judged by the same lock+argv check as any other
        board, which reads `none` - unblocked, unowned, `no-dispatcher` - not a silent pass-through
        to today's capacity gates."""
        home = home_with("my-app", "legacy", ran_tick=False)
        no_ps = lambda b, h: tick_guard.dispatcher(h, b, run_cmd=NO_TICKS)
        self.assertEqual(unblocked.dispatcher_state("my-app", home, check=no_ps), "none")
        items = unblocked.detect(capsule_home=home, now=2000,
                                  dispatcher_check=no_ps)
        it = next(i for i in items if i.source == "board:my-app" and "waiting" in i.item)
        self.assertEqual((it.state, it.owner, it.blocker), ("unblocked", "", "no-dispatcher"))

    def test_disagreement_is_blocked_uncertain(self):
        it = self.item(home_with("capsule-corp", "per-board"), "capsule-corp", "uncertain")
        self.assertEqual((it.state, it.blocker), ("blocked", "dispatcher-uncertain"))

    def test_mismatched_board_file_is_skipped(self):
        home = home_with("capsule-corp", "per-board")
        cfg = {"board": "my-app", "caps": {"max_running": 1}}
        paths.config_path(home, "capsule-corp").write_text(json.dumps(cfg))
        self.assertNotIn("capsule-corp", unblocked.load_caps_all(home))

    def test_per_board_unroutable_assignee_is_blocked_unassigned(self):
        """Fix round 1, F2/row 492: a per-board board's own `assignees`/`routing` (now carried
        by `dispatch_gate.caps_from`, same as the legacy board) still gates `unassigned` - it
        must never go silent just because the board is served through its own config file."""
        home = home_with("capsule-corp", "per-board")
        p = paths.board_db("capsule-corp", home)
        conn = db.connect(p)
        conn.execute("UPDATE tasks SET assignee='nobody' WHERE title='waiting'")
        conn.commit(); conn.close()
        it = self.item(home, "capsule-corp", "live")
        self.assertEqual((it.state, it.blocker), ("blocked", "unassigned"))

    def test_a_board_with_no_assignees_configured_at_all_still_blocks_unassigned(self):
        """Round 2 re-check R1-F1: main's own ruling (`test_unblocked.py:20-22`) applies to a
        board whose config carries no `assignees`/`routing` block at all, legacy or per-board
        alike - the round-0 `caps.get("assignees") and` guard must never come back unnoticed."""
        for cfg_where in ("legacy", "per-board"):
            with self.subTest(cfg_where=cfg_where):
                home = home_with("capsule-corp", cfg_where, routing=False)
                it = self.item(home, "capsule-corp", "live")
                self.assertEqual((it.state, it.blocker), ("blocked", "unassigned"))

    def test_detect_no_longer_takes_the_old_home_kwarg(self):
        """D244: the old board fallback is gone, so the kwarg is too."""
        with self.assertRaises(TypeError):
            unblocked.detect(capsule_home=Path(tempfile.mkdtemp()), legacy_home=Path("/no-home"))


class DispatcherStates(unittest.TestCase):
    """tick_guard.dispatcher: oracle 1 the flock, oracle 2 the argv scan; either alone is uncertain."""

    def test_the_lock_and_the_argv_scan_must_agree(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        me = os.getpid()
        mine = f"python3 -u -m capsule tick --board other --capsule-home {home}"
        self.assertEqual(tick_guard.dispatcher(home, "other", run_cmd=NO_TICKS), "none")
        self.assertEqual(tick_guard.dispatcher(home, "other", run_cmd=ps((me, mine))), "uncertain")  # argv, no lock
        lock = tick_guard.guard_start(home, "other")                     # writes this pid into tick-other.lock
        self.addCleanup(lock.close)
        self.assertEqual(tick_guard.dispatcher(home, "other", run_cmd=ps((me, mine))), "live")
        self.assertEqual(tick_guard.dispatcher(home, "other", run_cmd=NO_TICKS), "uncertain")      # lock, no process
        other_board = f"python3 -u -m capsule tick --board my-app --capsule-home {home}"
        other_home = "python3 -u -m capsule tick --board other --capsule-home /nonexistent-capsule-home"
        for argv in (other_board, other_home):
            with self.subTest(argv=argv):
                self.assertEqual(tick_guard.dispatcher(home, "other", run_cmd=ps((me, argv))), "uncertain")
        down = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")
        self.assertEqual(tick_guard.dispatcher(home, "other", run_cmd=down), "uncertain")          # ps unobservable
