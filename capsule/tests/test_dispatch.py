import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from capsule import config, db, dispatch, incidents, outbox, paths, workspace

CFG = config.load(paths.REPO / "capsule" / "config.example.json")
EPIC = CFG["epics"][0]

def board(tc):
    d = Path(tempfile.mkdtemp()); db.create_board(d / "k.db"); conn = db.connect(d / "k.db")
    tc.addCleanup(conn.close)   # R28: close every connection this helper opens (ResourceWarning)
    return conn

def ids(rows): return [r["id"] for r in rows]

class Dispatch(unittest.TestCase):
    def test_serial_within_an_epic(self):
        conn = board(self); a = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (a,))
        self.assertEqual(dispatch.ready(conn, CFG, host_tiers={"strong"}), [])

    def test_ready_skips_a_lane_whose_clean_backup_failed_incident_is_open(self):
        conn = board(self)
        a = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        self.assertEqual(self._ready_ids(conn), [a])
        incidents.open_incident(conn, board="b", kind="silent_failure", evidence=["x"],
                                key=f"epic clean backup failed: {EPIC}", now=5)
        self.assertEqual(self._ready_ids(conn), [])
        conn.execute("ALTER TABLE incidents RENAME TO incidents_gone")   # unreadable: fail open
        self.assertEqual(self._ready_ids(conn), [a])

    def test_overlapping_file_sets_across_epics_do_not_wait(self):
        cfg = dict(CFG, epics=[EPIC, "epic/other"], caps=dict(CFG["caps"], max_running=5))
        conn = board(self); a = db.new_task(conn, title="a", assignee="dev-frontend", branch="epic/other", file_set=["static/*"])
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (a,))
        b = db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC,
                        file_set=["static/timer.html"])
        self.assertEqual(ids(dispatch.ready(conn, cfg, host_tiers={"strong"})), [b])

    def _card_with_run(self, conn, status, file_set):
        a = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC, file_set=file_set)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, a))
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'x', 1)", (a,))
        return a

    def _ready_ids(self, conn):
        return ids(dispatch.ready(conn, CFG, host_tiers={"strong"}))

    def test_overlapping_card_waits_then_runs_once_the_first_is_done(self):
        conn = board(self)
        a = self._card_with_run(conn, "blocked", ["static/timer.html", "tests/ui/test_timer.py"])
        b = db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC,
                        file_set=["static/timer.html"])
        self.assertEqual(self._ready_ids(conn), [])
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (a,))
        self.assertEqual(self._ready_ids(conn), [b])

    def test_ready_card_with_a_run_holds_back_an_overlapping_card(self):
        conn = board(self)
        a = self._card_with_run(conn, "ready", ["static/timer.html"])
        b = db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC,
                        file_set=["static/timer.html"])
        conn.execute("UPDATE tasks SET priority=5 WHERE id=?", (b,))
        self.assertEqual(self._ready_ids(conn), [a])

    def test_triage_card_with_a_run_holds_back_an_overlapping_card(self):
        conn = board(self)
        self._card_with_run(conn, "triage", ["static/timer.html"])
        db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC,
                    file_set=["static/timer.html"])
        self.assertEqual(self._ready_ids(conn), [])

    def test_hold_reason_is_clone_dirty_behind_an_overlapping_card_that_ran(self):
        from capsule import unblocked
        conn = board(self)
        a = self._card_with_run(conn, "blocked", ["static/timer.html"])
        b = db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC,
                        file_set=["static/timer.html"])
        card = conn.execute("SELECT * FROM tasks WHERE id=?", (b,)).fetchone()
        self.assertEqual(unblocked.dispatcher_hold_reason(conn, card, set()),
                         f"clone-dirty (held by {a})")

    def test_two_ready_cards_with_runs_dispatch_exactly_one(self):
        conn = board(self)
        a = self._card_with_run(conn, "ready", ["static/timer.html"])
        b = self._card_with_run(conn, "ready", ["static/timer.html"])
        conn.execute("UPDATE tasks SET created_at=1 WHERE id=?", (a,))
        conn.execute("UPDATE tasks SET created_at=2 WHERE id=?", (b,))
        self.assertEqual(self._ready_ids(conn), [b])

    def _two_ready_holders(self, conn):
        a = self._card_with_run(conn, "ready", ["static/timer.html"])
        b = self._card_with_run(conn, "ready", ["static/timer.html"])
        conn.execute("UPDATE tasks SET created_at=1 WHERE id=?", (a,))
        conn.execute("UPDATE tasks SET created_at=2 WHERE id=?", (b,))
        return a, b

    def test_the_ready_card_with_the_newest_run_goes_before_an_older_ready_holder(self):
        conn = board(self)
        a, b = self._two_ready_holders(conn)
        self.assertEqual(self._ready_ids(conn), [b])
        self.assertEqual(dispatch.clone_dirty(conn, EPIC, a), b)
        self.assertIsNone(dispatch.clone_dirty(conn, EPIC, b))

    def test_the_older_card_goes_first_when_it_holds_the_newest_run(self):
        conn = board(self)
        a, b = self._two_ready_holders(conn)
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'x', 3)", (a,))
        self.assertEqual(self._ready_ids(conn), [a])
        self.assertEqual(dispatch.clone_dirty(conn, EPIC, b), a)

    def test_the_older_ready_card_goes_once_the_newer_run_card_is_done(self):
        conn = board(self)
        a, b = self._two_ready_holders(conn)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (b,))
        self.assertEqual(self._ready_ids(conn), [a])
        self.assertIsNone(dispatch.clone_dirty(conn, EPIC, a))

    def test_a_ready_holder_with_a_run_outranks_one_with_no_run(self):
        # seeds a defensive state (ready + claim_lock, no run) the dispatcher must order
        conn = board(self)
        a = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        b = self._card_with_run(conn, "ready", [])
        conn.execute("UPDATE tasks SET claim_lock='x', created_at=1 WHERE id=?", (a,))
        conn.execute("UPDATE tasks SET created_at=2 WHERE id=?", (b,))
        self.assertEqual(dispatch.clone_dirty(conn, EPIC, a), b)
        self.assertIsNone(dispatch.clone_dirty(conn, EPIC, b))

    def test_two_no_run_holders_exactly_one_goes(self):
        # seeds a defensive state (ready + claim_lock, no run) the dispatcher must order
        conn = board(self)
        a = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        b = db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC)
        conn.execute("UPDATE tasks SET claim_lock='x', created_at=1 WHERE id=?", (a,))
        conn.execute("UPDATE tasks SET claim_lock='y', created_at=2 WHERE id=?", (b,))
        self.assertEqual(self._ready_ids(conn), [a])
        self.assertEqual(dispatch.clone_dirty(conn, EPIC, b), a)
        self.assertIsNone(dispatch.clone_dirty(conn, EPIC, a))

    def test_disjoint_or_empty_file_set_card_waits_for_a_blocked_card_on_its_epic(self):
        conn = board(self)
        a = self._card_with_run(conn, "blocked", ["static/timer.html"])
        db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC,
                    file_set=["static/other.css"])
        db.new_task(conn, title="c", assignee="dev-frontend", branch=EPIC)
        self.assertEqual(self._ready_ids(conn), [])
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (a,))
        self.assertEqual(len(self._ready_ids(conn)), 2)

    def test_empty_file_sets_card_waits_for_a_review_card_on_its_epic(self):
        # regression-only: passes on the old code too
        conn = board(self)
        self._card_with_run(conn, "review", [])
        db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC)
        self.assertEqual(self._ready_ids(conn), [])

    def test_other_epic_card_is_not_delayed_by_a_blocked_card(self):
        # regression-only: passes on the old code too
        cfg = dict(CFG, epics=[EPIC, "epic/other"], caps=dict(CFG["caps"], max_running=5))
        conn = board(self)
        self._card_with_run(conn, "blocked", [])
        b = db.new_task(conn, title="b", assignee="dev-frontend", branch="epic/other")
        self.assertEqual(ids(dispatch.ready(conn, cfg, host_tiers={"strong"})), [b])

    def test_blocked_card_with_a_settled_run_does_not_hold_its_epic(self):
        conn = board(self)
        old = self._card_with_run(conn, "blocked", [])
        done = self._card_with_run(conn, "done", [])
        conn.execute("UPDATE tasks SET claim_lock='stale' WHERE id=?", (done,))
        b = db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC)
        self.assertEqual(self._ready_ids(conn), [b])
        self.assertIsNone(dispatch.clone_dirty(conn, EPIC, b))
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'x', 2)", (old,))
        self.assertEqual(dispatch.clone_dirty(conn, EPIC, b), old)
        self.assertEqual(self._ready_ids(conn), [])

    def test_unrouted_assignee_skipped(self):
        conn = board(self); db.new_task(conn, title="a", assignee="qa", branch=EPIC)
        ok = db.new_task(conn, title="b", assignee="dev-backend", branch=EPIC)
        self.assertEqual(ids(dispatch.ready(conn, CFG, host_tiers={"strong"})), [ok])

    def test_strong_waits_without_docker(self):
        conn = board(self); db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        self.assertEqual(dispatch.ready(conn, CFG, host_tiers=set()), [])

    def test_board_pause_holds_until_window_ends(self):
        conn = board(self); t = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        db.guard_set(conn, "pause:board", {"until": 1000, "reason": "rate limit"})
        self.assertEqual(dispatch.ready(conn, CFG, host_tiers={"strong"}, now=999), [])
        self.assertEqual(ids(dispatch.ready(conn, CFG, host_tiers={"strong"}, now=1000)), [t])

class DepsReady(unittest.TestCase):
    """S5 (redesign.deps_ready): a ready card with an unlanded `task_links` parent is not ready
    while the switch is on; switch off is today's `ready` (the claim bounces it instead)."""

    def cfg(self, on):
        return dict(CFG, redesign={"deps_ready": on})

    def ready(self, conn, on):
        return ids(dispatch.ready(conn, self.cfg(on), host_tiers={"strong"}))

    def card(self, conn, name, **kw):
        kw.setdefault("assignee", "dev-frontend")
        kw.setdefault("branch", EPIC)
        return db.new_task(conn, title=name, **kw)

    def give_run(self, conn, tid):
        sql = "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'x', 1)"
        conn.execute(sql, (tid,))

    def test_unlanded_parent_holds_the_child_until_it_lands(self):
        conn = board(self)
        parent = self.card(conn, "parent", status="todo")
        child = self.card(conn, "child", parents=[parent])
        self.assertEqual(self.ready(conn, True), [])
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        self.assertEqual(self.ready(conn, True), [child])

    def test_archived_parent_does_not_hold(self):
        conn = board(self)
        parent = self.card(conn, "parent", status="archived")
        child = self.card(conn, "child", parents=[parent])
        self.assertEqual(self.ready(conn, True), [child])

    def test_a_held_child_does_not_hide_an_independent_card(self):
        conn = board(self)
        parent = self.card(conn, "parent", status="todo")
        self.card(conn, "child", parents=[parent])
        free = self.card(conn, "free")
        self.assertEqual(self.ready(conn, True), [free])

    def test_switch_off_or_absent_returns_the_held_child_as_today(self):
        conn = board(self)
        parent = self.card(conn, "parent", status="todo")
        child = self.card(conn, "child", parents=[parent])
        self.assertEqual(self.ready(conn, False), [child])
        self.assertEqual(ids(dispatch.ready(conn, CFG, host_tiers={"strong"})), [child])

    def test_overlapping_file_sets_never_run_together_with_the_switch_on(self):
        conn = board(self)
        a = self.card(conn, "a", file_set=["static/timer.html"])
        b = self.card(conn, "b", file_set=["static/timer.html"])
        conn.execute("UPDATE tasks SET created_at=1 WHERE id=?", (a,))
        for tid in (a, b):
            self.give_run(conn, tid)
        self.assertEqual(self.ready(conn, True), [b])
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (b,))
        self.assertEqual(self.ready(conn, True), [])

    def run_row(self, doc):
        """One `deps_ready` intent row: cards (name, status, parents, file_set, run) built in
        order, then `ready` with the switch as given; returns the ready card names."""
        conn = board(self)
        names = {}
        for i, spec in enumerate(doc["cards"]):
            tid = self.card(conn, spec["name"], status=spec.get("status", "ready"), now=100 + i,
                            parents=[names[p] for p in spec.get("parents", [])],
                            file_set=spec.get("file_set", []))
            names[spec["name"]] = tid
            if spec.get("run"):
                self.give_run(conn, tid)
        by_id = {v: k for k, v in names.items()}
        return [by_id[i] for i in self.ready(conn, doc["switch"] == "on")]

    def test_each_intent_row_gives_the_expected_ready_cards(self):
        from capsule import intent
        rows = intent.load("deps_ready")
        self.assertTrue(rows, "deps_ready intent rows missing")
        for ex in rows:
            doc = ex["given"]["document"]
            self.assertEqual(self.run_row(doc), ex["expect"]["ready"], ex["id"])

class RunningCount(unittest.TestCase):
    """Row 483 fix round 1, I1: `dispatch.running_count` is the one shared definition of
    "how many cards are actively running" - `ready` and `capsule.unblocked`'s capacity
    blocker must never drift onto two different queries again."""

    def test_a_plain_running_task_counts(self):
        conn = board(self)
        a = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (a,))
        self.assertEqual(dispatch.running_count(conn, now=1000), 1)

    def test_an_expired_claim_does_not_count(self):
        conn = board(self)
        a = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        conn.execute("UPDATE tasks SET status='running', claim_expires=? WHERE id=?", (500, a))
        self.assertEqual(dispatch.running_count(conn, now=1000), 0)

    def test_a_claim_expiring_exactly_now_still_counts(self):
        conn = board(self)
        a = db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC)
        conn.execute("UPDATE tasks SET status='running', claim_expires=? WHERE id=?", (1000, a))
        self.assertEqual(dispatch.running_count(conn, now=1000), 1)

    def test_a_non_running_task_never_counts(self):
        conn = board(self)
        db.new_task(conn, title="a", assignee="dev-frontend", branch=EPIC, status="review")
        self.assertEqual(dispatch.running_count(conn, now=1000), 0)

    def test_ready_itself_ignores_a_running_task_whose_claim_already_expired(self):
        """The consequence for `ready` (not only the standalone helper): a card the
        dispatcher would in fact take now that a *different-branch* predecessor's claim has
        lapsed, even before `release_stale` gets around to sweeping that row back to `ready`
        - `max_running=1` would otherwise hold the whole board shut on a stale row alone.
        (Different branches so the pre-existing serial-within-branch rule, tested by
        `test_serial_within_an_epic`, never enters into it.)"""
        cfg = dict(CFG, epics=[EPIC, "epic/other"], caps=dict(CFG["caps"], max_running=1))
        conn = board(self)
        a = db.new_task(conn, title="a", assignee="dev-frontend", branch="epic/other")
        conn.execute("UPDATE tasks SET status='running', claim_expires=? WHERE id=?", (500, a))
        b = db.new_task(conn, title="b", assignee="dev-frontend", branch=EPIC)
        self.assertEqual(ids(dispatch.ready(conn, cfg, host_tiers={"strong"}, now=1000)), [b])


class BusyBranches(unittest.TestCase):
    """The shared read `ready`'s serial-branch rule builds from, exposed so
    `capsule.unblocked` can apply the identical per-card gate."""

    def test_running_and_review_branches_are_busy_not_ready_or_done(self):
        conn = board(self)
        db.new_task(conn, title="running", assignee="dev-frontend", branch="epic/a", status="running")
        db.new_task(conn, title="review", assignee="dev-frontend", branch="epic/b", status="review")
        db.new_task(conn, title="ready", assignee="dev-frontend", branch="epic/c", status="ready")
        db.new_task(conn, title="done", assignee="dev-frontend", branch="epic/d", status="done")
        self.assertEqual(dispatch.busy_branches(conn), {"epic/a", "epic/b"})


class BranchBusyFn(unittest.TestCase):
    def test_true_when_branch_is_in_the_busy_set(self):
        self.assertTrue(dispatch.branch_busy("epic/a", {"epic/a"}))

    def test_false_when_branch_is_not_in_the_busy_set(self):
        self.assertFalse(dispatch.branch_busy("epic/a", {"epic/b"}))


class ScreenHeldFn(unittest.TestCase):
    """Row 483 follow-up review round 1, finding 2: `ready`'s own first-screen gate
    (`blocks.first_screen`, row 369/R27), shared so `capsule.unblocked` can apply it too."""

    def test_false_with_no_hold_at_all(self):
        conn = board(self)
        self.assertFalse(dispatch.screen_held(conn, "epic/a", "t1"))

    def test_true_while_held_outright(self):
        conn = board(self)
        db.guard_set(conn, "first_screen:epic/a", {"state": "held", "task_id": "t1"})
        self.assertTrue(dispatch.screen_held(conn, "epic/a", "t1"))
        self.assertTrue(dispatch.screen_held(conn, "epic/a", "t2"))

    def test_true_on_fix_only_for_a_different_card(self):
        conn = board(self)
        db.guard_set(conn, "first_screen:epic/a", {"state": "fix", "task_id": "t1"})
        self.assertFalse(dispatch.screen_held(conn, "epic/a", "t1"))
        self.assertTrue(dispatch.screen_held(conn, "epic/a", "t2"))

    def test_false_once_answered_ok(self):
        conn = board(self)
        db.guard_set(conn, "first_screen:epic/a", {"state": "ok", "task_id": "t1"})
        self.assertFalse(dispatch.screen_held(conn, "epic/a", "t1"))
        self.assertFalse(dispatch.screen_held(conn, "epic/a", "t2"))


class BoardPaused(unittest.TestCase):
    def test_true_while_the_pause_window_holds(self):
        conn = board(self)
        db.guard_set(conn, "pause:board", {"until": 1000, "reason": "rate limit"})
        self.assertTrue(dispatch.board_paused(conn, now=999))

    def test_false_once_the_pause_window_ends(self):
        conn = board(self)
        db.guard_set(conn, "pause:board", {"until": 1000, "reason": "rate limit"})
        self.assertFalse(dispatch.board_paused(conn, now=1000))

    def test_false_with_no_guard_at_all(self):
        conn = board(self)
        self.assertFalse(dispatch.board_paused(conn, now=1000))


class EpicBranchesFromListFn(unittest.TestCase):
    """Row 483 follow-up RR3: the one tolerant reader both `config.epic_branches` (a
    validated config) and `capsule.unblocked.load_caps` (the raw, un-validated config
    document) share, so the two can never drift onto two different notions of "what branch
    does this epics[] entry name" the way `unblocked._epic_branches_from_raw` used to."""

    def test_string_and_object_entries(self):
        self.assertEqual(
            config.epic_branches_from_list(["epic/a", {"branch": "epic/b", "ui": True}]),
            ["epic/a", "epic/b"])

    def test_malformed_entries_are_skipped_not_raised(self):
        self.assertEqual(
            config.epic_branches_from_list(["epic/good", 5, {"no_branch": "x"}, {"branch": ""}, None]),
            ["epic/good"])

    def test_not_a_list_at_all_is_empty(self):
        self.assertEqual(config.epic_branches_from_list(None), [])
        self.assertEqual(config.epic_branches_from_list("epic/a"), [])

    def test_epic_branches_skips_malformed_entries_like_the_shared_reader(self):
        """Review round 1, finding 4: the previous version of this test only compared
        `config.epic_branches` against `epic_branches_from_list` over already-`validate`d
        input (`CFG`), so reverting `epic_branches` back to its old inline comprehension
        (`e["branch"] if isinstance(e, dict) else e`, no skipping) left this fully green - a
        tautology, since `CFG["epics"]` never contains a malformed entry. A raw,
        un-validated cfg with malformed entries proves `epic_branches` is genuinely calling
        the shared, tolerant reader and not a copy of the old logic (which would raise a
        `KeyError` on `{"no_branch": "x"}` instead of skipping it)."""
        cfg = {"epics": ["epic/good", 5, {"no_branch": "x"}, {"branch": ""}, None]}
        self.assertEqual(config.epic_branches(cfg), ["epic/good"])

    def test_load_normalises_epics_with_the_shared_reader(self):
        """Review round 1, finding 5: `config.load` no longer keeps its own inline copy of
        "branch of an entry" - it calls `epic_branches_from_list` too."""
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps(dict(CFG, epics=["epic/a", {"branch": "epic/b", "ui": True}])))
        loaded = config.load(p)
        self.assertEqual(loaded["epics"], config.epic_branches_from_list(["epic/a", {"branch": "epic/b", "ui": True}]))


class Config(unittest.TestCase):
    def test_broken_file_keeps_last_good_one_line(self):
        p = Path(tempfile.mkdtemp()) / "c.json"; p.write_text(json.dumps(CFG))
        ld = config.Loader(p); good, err = ld.get(); self.assertIsNone(err)
        p.write_text("{broken"); c1, e1 = ld.get(); c2, e2 = ld.get()
        self.assertEqual((c1, c2), (good, good)); self.assertIsNotNone(e1); self.assertIsNone(e2)
        p.write_text(json.dumps(dict(CFG, caps=dict(CFG["caps"], max_running=2))))
        self.assertEqual(ld.get()[0]["caps"]["max_running"], 2)   # re-read per tick (row 377)

    def test_missing_routing_role_is_named(self):
        with self.assertRaisesRegex(ValueError, "routing.implementer"):
            config.validate(dict(CFG, routing={}))

    def test_runtime_caps_default_when_absent(self):
        """R23: `caps.max_runtime_seconds` and `caps.review_seconds` are optional with defaults."""
        caps = {k: v for k, v in CFG["caps"].items() if k not in config.CAP_DEFAULTS}
        cfg = config.validate(dict(CFG, caps=caps))
        self.assertEqual((config.cap(cfg, "max_runtime_seconds"), config.cap(cfg, "review_seconds")), (3600, 1800))
        cfg = config.validate(dict(CFG, caps=dict(caps, review_seconds=60)))
        self.assertEqual(config.cap(cfg, "review_seconds"), 60)

    def test_a_bad_runtime_cap_is_named(self):
        with self.assertRaisesRegex(ValueError, "caps.max_runtime_seconds"):
            config.validate(dict(CFG, caps=dict(CFG["caps"], max_runtime_seconds=0)))
        with self.assertRaisesRegex(ValueError, "caps.review_seconds"):
            config.validate(dict(CFG, caps=dict(CFG["caps"], review_seconds="30")))

class Outbox(unittest.TestCase):
    def test_retry_then_deliver_once(self):
        conn = board(self); outbox.add(conn, "file", "hello"); calls = []
        def send(row):
            calls.append(row["id"])
            if len(calls) == 1: raise OSError("down")
        self.assertEqual(outbox.flush(conn, send), 0); self.assertEqual(outbox.flush(conn, send), 1)
        self.assertEqual(outbox.flush(conn, send), 0); self.assertEqual(len(calls), 2)
        row = conn.execute("SELECT attempts, delivered_at IS NOT NULL, last_error FROM outbox").fetchone()
        self.assertEqual(tuple(row), (2, 1, "down"))


class WaitingHolder(unittest.TestCase):
    """A ready holder that waits on the card (directly or through a chain) holds the clone only
    while the clone has uncommitted work: that work is the holder's, not the card's."""

    def setUp(self):
        self.conn = board(self)
        d = Path(tempfile.mkdtemp())
        origin = d / "origin"
        origin.mkdir()
        self.root = d / "work"
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        for cmd in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "i"],
                    ["branch", EPIC]):
            subprocess.run(g + cmd, cwd=origin, check=True, capture_output=True)
        self.ws = workspace.clone(str(origin), EPIC, self.root)
        self.parent = db.new_task(self.conn, title="p", assignee="dev-frontend", branch=EPIC)

    def link(self, parent, child):
        self.conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)",
                          (parent, child))

    def holder(self, parent_of):
        c = db.new_task(self.conn, title="c", assignee="dev-frontend", branch=EPIC)
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (c,))
        self.conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'x', 1)", (c,))
        self.link(parent_of, c)
        return c

    def holder_of_parent(self):
        return dispatch.clone_dirty(self.conn, EPIC, self.parent, self.root)

    def test_clean_clone_lets_the_parent_go(self):
        self.holder(self.parent)
        self.assertIsNone(self.holder_of_parent())

    def test_grandchild_holder_on_a_clean_clone_lets_the_parent_go(self):
        child = db.new_task(self.conn, title="m", assignee="dev-frontend", branch=EPIC)
        self.link(self.parent, child)
        self.holder(child)
        self.assertIsNone(self.holder_of_parent())

    def test_a_dependency_cycle_terminates(self):
        c = self.holder(self.parent)
        self.link(c, self.parent)
        self.assertIsNone(self.holder_of_parent())

    def test_dirty_clone_still_holds_the_parent_back(self):
        c = self.holder(self.parent)
        (self.ws / "stray.txt").write_text("the child's work\n")
        self.assertEqual(self.holder_of_parent(), c)

    def test_ready_passes_the_boards_work_root(self):
        home = Path(tempfile.mkdtemp())
        with mock.patch.object(dispatch, "clone_dirty", return_value=None) as cd:
            dispatch.ready(self.conn, CFG, host_tiers={"strong"}, home=home)
        root = paths.board_dir(CFG["board"], home) / "work"
        cd.assert_called_with(self.conn, EPIC, self.parent, root)

    def test_no_clone_status_call_without_a_ready_candidate(self):
        with mock.patch.object(dispatch, "_clone_clean") as cc:
            dispatch.clone_dirty(self.conn, EPIC, self.parent, self.root)
            self.holder(self.parent)
            self.conn.execute("UPDATE tasks SET status='blocked' WHERE status='ready' AND id!=?",
                              (self.parent,))
            dispatch.clone_dirty(self.conn, EPIC, self.parent, self.root)
        cc.assert_not_called()

    def test_no_work_root_keeps_holding(self):
        c = self.holder(self.parent)
        self.assertEqual(dispatch.clone_dirty(self.conn, EPIC, self.parent), c)
