# mutation-check: targets-predate-range b957739c9 -- renames and an absence guard, no behaviour ::
#   BoardAdapter.test_card_body_waits_owner_is_ignored_and_stays_unblocked,
#   BoardAdapter.test_comment_waits_owner_is_ignored_and_stays_unblocked,
#   BranchAdapter.test_queue_json_marker_waits_owner_is_ignored_and_stays_unblocked,
#   Config.test_owner_holds_defaults_to_empty,
#   Config.test_owner_holds_reads_a_list_of_strings,
#   Detect.test_owner_holds_forces_a_named_item_blocked,
#   EvaluateBlocker.test_no_private_tool_has_a_running_check_in_unblocked,
#   EvaluateBlocker.test_owner_blocks_only_when_explicitly_allowed,
#   EvaluateBlocker.test_owner_does_not_block_by_default,
#   HumanAdapter.test_owner_only_verb_is_blocked,
#   IntentExamples.test_every_example_matches_its_oracle,
#   ParseWaits.test_case_and_whitespace_tolerant,
#   ParseWaits.test_last_waits_line_wins,
#   LegacyHomeDefault.test_default_is_dot_legacy_under_home,
#   LegacyHomeDefault.test_env_override_wins,
#   Config.test_the_old_waits_word_still_blocks_like_owner
"""`capsule unblocked` (row 470): a deterministic detector of unblocked, unowned work.

Fixture tests for each adapter, the shared blocker/ownership judge, and the summary/exit
contract. Every DB fixture is opened the same read-only way the module itself opens a
live board; every git fixture is a throwaway repo under a tempdir.
"""
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from capsule import db, human, paths, unblocked
from capsule.tests.private_names import DENYLIST, SKIP_MSG, project_names
from capsule.tests.test_unblocked_one_store import c25_example_holds
from capsule.tests.fixtures.board_ddl import board_ddl
from capsule import legacy_names as _LN  # noqa: E402

EPIC = "epic/fixture-branch"    # row 483 fix round 1, M2: capacity/no-epic fixtures' epic branch
ASSIGNEE = "impl-1"              # a routable assignee (see ROUTING) for fixtures that must get
# past the `unassigned` check to reach the blocker they actually test (no-epic/capacity/
# dispatcher-hold/board-paused-then-capacity) - `board_items` now applies `resolve_assignee`
# to every `caps`, not just ones that happen to carry `assignees`/`routing`.
ROUTING = {"assignees": {ASSIGNEE: "implementer"}, "routing": {"implementer": {}}}


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


def caps(max_running, epic_branches, **extra):
    return {"max_running": max_running, "epic_branches": epic_branches, **ROUTING, **extra}


class ParseWaits(unittest.TestCase):
    def test_no_waits_line_is_none(self):
        self.assertIsNone(unblocked.parse_waits("just a plain card body\nwith no marker"))

    def test_last_waits_line_wins(self):
        text = "notes\nwaits: other-app\nmore notes\nwaits: owner\n"
        self.assertEqual(unblocked.parse_waits(text), "owner")

    def test_case_and_whitespace_tolerant(self):
        self.assertEqual(unblocked.parse_waits("  WAITS:   owner  "), "owner")

    def test_none_text_is_none(self):
        self.assertIsNone(unblocked.parse_waits(None))


class EvaluateBlocker(unittest.TestCase):
    def test_report_commit_blocked_while_that_run_is_running(self):
        blocked, blocker = unblocked.evaluate_blocker(
            "report-commit abc1234", run_status_by_commit={"abc1234": ["running"]})
        self.assertTrue(blocked)
        self.assertIn("abc1234", blocker)

    def test_report_commit_not_blocked_once_that_run_is_done(self):
        blocked, _ = unblocked.evaluate_blocker(
            "report-commit abc1234", run_status_by_commit={"abc1234": ["done"]})
        self.assertFalse(blocked)

    def test_report_commit_unknown_sha_is_not_blocked(self):
        blocked, _ = unblocked.evaluate_blocker(
            "report-commit deadbee", run_status_by_commit={})
        self.assertFalse(blocked)

    def test_report_commit_matches_case_insensitively(self):
        """Row 470 fix round 1, I5."""
        blocked, _ = unblocked.evaluate_blocker(
            "report-commit ABC1234", run_status_by_commit={"abc1234": ["running"]})
        self.assertTrue(blocked)

    def test_report_commit_matches_as_a_prefix_of_the_full_sha(self):
        """Row 470 fix round 1, I5: a 7-char `waits:` sha is a prefix of the full sha a
        progress.json actually records."""
        blocked, _ = unblocked.evaluate_blocker(
            "report-commit abc1234",
            run_status_by_commit={"abc1234def5670000000000000000000000000": ["running"]})
        self.assertTrue(blocked)

    def test_report_commit_blocks_if_any_matching_run_is_running(self):
        """Row 470 fix round 1, I5: the same commit can recur across more than one run
        file (a re-run) - one of them running is enough to block, even if another one
        already finished."""
        blocked, _ = unblocked.evaluate_blocker(
            "report-commit abc1234", run_status_by_commit={"abc1234": ["done", "running"]})
        self.assertTrue(blocked)

    def test_other_app_wait_is_unblocked_like_any_unknown_text(self):
        """D190: the old `other-app` kind is gone. `waits: other-app` reads as
        unblocked whether or not an other-app process exists, exactly like unknown text."""
        self.assertEqual(unblocked.evaluate_blocker(
            "other-app", run_status_by_commit={}), (False, ""))

    def test_no_private_tool_has_a_running_check_in_unblocked(self):
        """The retired `<tool>_running` kinds stay gone: no private project name from the
        denylist names a function in unblocked (a literal name here would itself be a hit)."""
        if not DENYLIST.is_file():
            self.skipTest(SKIP_MSG)
        for name in project_names():
            self.assertFalse(hasattr(unblocked, f"{name}_running"), name)

    def test_owner_blocks_only_when_explicitly_allowed(self):
        """Row 470 fix round 1, I3: the allow flag gates the owner rule - only a
        human-verb file or a config owner-holds entry (the only two callers that ever
        pass True) may make an owner wait actually block."""
        blocked, blocker = unblocked.evaluate_blocker(
            "owner", run_status_by_commit={}, allow_owner=True)
        self.assertTrue(blocked)
        self.assertEqual(blocker, "waits: owner")

    def test_owner_does_not_block_by_default(self):
        """Row 470 fix round 1, I3: a board card body/comment or a branch queue marker -
        every caller that does not pass the allow flag as True - must never be able to
        self-block on the owner; an owner wait from one of those reads as unrecognised."""
        blocked, blocker = unblocked.evaluate_blocker(
            "owner", run_status_by_commit={})
        self.assertFalse(blocked)
        self.assertEqual(blocker, "")

    def test_unrecognised_waits_never_blocks(self):
        blocked, _ = unblocked.evaluate_blocker(
            "some other reason entirely", run_status_by_commit={})
        self.assertFalse(blocked)

    def test_no_waits_is_unblocked(self):
        self.assertEqual(unblocked.evaluate_blocker(
            None, run_status_by_commit={}), (False, ""))


class CapacityBlockedFn(unittest.TestCase):
    """Row 483: the pure arithmetic oracle `board_items` applies - the second oracle is the
    end-to-end sqlite fixture in `BoardAdapter` below, which shares no code path with this."""

    def test_true_once_running_reaches_the_cap(self):
        self.assertTrue(unblocked.capacity_blocked(1, 1))

    def test_true_past_the_cap(self):
        self.assertTrue(unblocked.capacity_blocked(2, 1))

    def test_false_below_the_cap(self):
        self.assertFalse(unblocked.capacity_blocked(0, 1))


class EpicBranchOkFn(unittest.TestCase):
    """Row 483 fix round 1, M2: the pure oracle `board_items` uses to decide whether
    `dispatch.ready` would consider a card at all - the second oracle is `BoardAdapter`'s
    own end-to-end sqlite fixtures below."""

    def test_true_when_branch_is_in_the_list(self):
        self.assertTrue(unblocked.epic_branch_ok("epic/a", ["epic/a", "epic/b"]))

    def test_false_when_branch_is_not_in_the_list(self):
        self.assertFalse(unblocked.epic_branch_ok("epic/c", ["epic/a", "epic/b"]))

    def test_false_when_branch_is_none(self):
        self.assertFalse(unblocked.epic_branch_ok(None, ["epic/a"]))

    def test_false_when_the_list_is_empty(self):
        self.assertFalse(unblocked.epic_branch_ok("epic/a", []))


class DispatcherHoldReasonFn(unittest.TestCase):
    """Row 483 follow-up RR2 (round 1 finding 2): the pure oracle `board_items` uses to
    decide whether `dispatch.ready`'s own per-card gates would hold an otherwise-unblocked,
    under-cap, on-epic ready card back this tick - built straight from `dispatch`'s own
    shared predicates (`dispatch.branch_busy`/`dispatch.screen_held`/
    `dispatch.clone_dirty`), never a re-derived copy of them. `conn` is only ever
    touched by `dispatch.screen_held` (reads `guard_state`); a fresh, empty board has none.
    The second oracle is `BoardAdapter`'s own end-to-end sqlite fixture below."""

    def _conn(self):
        d = Path(tempfile.mkdtemp()) / "k.db"
        _create_board(d)
        conn = db.connect(d)
        self.addCleanup(conn.close)
        return conn

    def test_branch_busy_wins(self):
        conn = self._conn()
        card = {"branch_name": "epic/a", "file_set": None, "id": "t1"}
        self.assertEqual(unblocked.dispatcher_hold_reason(conn, card, {"epic/a"}), "branch-busy")

    def test_screen_hold_is_reported(self):
        conn = self._conn()
        db.guard_set(conn, "first_screen:epic/a", {"state": "held", "task_id": "other"})
        card = {"branch_name": "epic/a", "file_set": json.dumps(["static/*"]), "id": "t1"}
        self.assertEqual(
            unblocked.dispatcher_hold_reason(conn, card, set()), "screen-hold")

    def test_screen_fix_for_this_same_card_does_not_hold_it(self):
        conn = self._conn()
        db.guard_set(conn, "first_screen:epic/a", {"state": "fix", "task_id": "t1"})
        card = {"branch_name": "epic/a", "file_set": None, "id": "t1"}
        self.assertIsNone(unblocked.dispatcher_hold_reason(conn, card, set()))

    def test_none_when_nothing_applies(self):
        conn = self._conn()
        card = {"branch_name": "epic/a", "file_set": None, "id": "t1"}
        self.assertIsNone(unblocked.dispatcher_hold_reason(conn, card, set()))

    def test_branch_busy_checked_before_clone_dirty(self):
        conn = self._conn()
        card = {"branch_name": "epic/a", "file_set": json.dumps(["static/*"]), "id": "t1"}
        self.assertEqual(
            unblocked.dispatcher_hold_reason(conn, card, {"epic/a"}), "branch-busy")


class LoadCaps(unittest.TestCase):
    def test_reads_board_and_max_running_from_the_top_level(self):
        home = Path(tempfile.mkdtemp())
        cfg = {"board": "my-app", "caps": {"max_running": 1}}
        (home / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(unblocked.load_caps(home),
                          {"board": "my-app", "max_running": 1, "epic_branches": [],
                           "assignees": {}, "routing": {}})

    def test_reads_assignees_and_routing_from_the_top_level(self):
        """Row 492: needed by the `unassigned` detector - the identical resolution
        `dispatch.resolve_assignee` performs."""
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({
            "board": "my-app", "caps": {"max_running": 1},
            "assignees": {"impl-1": "implementer"}, "routing": {"implementer": {}}}))
        caps = unblocked.load_caps(home)
        self.assertEqual(caps["assignees"], {"impl-1": "implementer"})
        self.assertEqual(caps["routing"], {"implementer": {}})

    def test_malformed_assignees_or_routing_reads_as_empty_not_an_error(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({
            "board": "my-app", "caps": {"max_running": 1}, "assignees": "not-a-dict",
            "routing": 5}))
        caps = unblocked.load_caps(home)
        self.assertEqual((caps["assignees"], caps["routing"]), ({}, {}))

    def test_reads_epic_branches_string_and_object_entries(self):
        """Row 483 fix round 1, M2."""
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({
            "board": "my-app", "caps": {"max_running": 1},
            "epics": ["epic/a", {"branch": "epic/b", "ui": True}]}))
        self.assertEqual(unblocked.load_caps(home)["epic_branches"], ["epic/a", "epic/b"])

    def test_missing_epics_is_an_empty_list_not_an_error(self):
        home = Path(tempfile.mkdtemp())
        cfg = {"board": "my-app", "caps": {"max_running": 1}}
        (home / "config.json").write_text(json.dumps(cfg))
        self.assertEqual(unblocked.load_caps(home)["epic_branches"], [])

    def test_malformed_epic_entries_are_skipped_not_an_error(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({
            "board": "my-app", "caps": {"max_running": 1},
            "epics": ["epic/good", 5, {"no_branch": "x"}, {"branch": ""}]}))
        self.assertEqual(unblocked.load_caps(home)["epic_branches"], ["epic/good"])

    def test_missing_file_is_none(self):
        self.assertIsNone(unblocked.load_caps(Path(tempfile.mkdtemp())))

    def test_no_board_or_caps_block_is_none(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": ["my-app"]}}))
        self.assertIsNone(unblocked.load_caps(home))

    def test_zero_or_negative_max_running_is_none(self):
        home = Path(tempfile.mkdtemp())
        cfg = {"board": "my-app", "caps": {"max_running": 0}}
        (home / "config.json").write_text(json.dumps(cfg))
        self.assertIsNone(unblocked.load_caps(home))

    def test_invalid_json_is_an_environment_error(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text("{not json")
        with self.assertRaises(unblocked.EnvironmentError_):
            unblocked.load_caps(home)


class BoardAdapter(unittest.TestCase):
    def test_ready_unassigned_card_is_an_unblocked_unowned_candidate(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="do the thing", status="ready", now=1000)
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=1000 + 3600,
                                       run_status_by_commit={})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "unblocked")
        self.assertEqual(items[0].owner, "")
        self.assertEqual(items[0].age_s, 3600)

    def test_ready_card_with_an_assignee_is_not_a_candidate_when_caps_is_none(self):
        """`caps is None`: a bare truthy `assignee` alone is enough (the pre-row-483 reading)."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="claimed", status="ready", assignee="dev-frontend")
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(items, [])

    def test_assigned_waits_blocked_card_is_still_not_a_candidate_when_caps_is_none(self):
        """`caps is None`: assigned + `waits:`-blocked is still omitted unconditionally, matching base."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="claimed and waiting", status="ready", assignee="dev-frontend",
                    body="waits: report-commit abc1234\n")
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={"abc1234": ["running"]})
        self.assertEqual(items, [])

    def test_built_card_with_a_live_run_is_owned_not_a_candidate(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="in flight", status="built")
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?,?,?)",
                     (tid, "running", 1))
        conn.commit()
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(items, [])

    def test_built_card_with_no_live_run_is_a_candidate(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="stuck in review", status="review")
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?,?,?)",
                     (tid, "done", 1))
        conn.commit()
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner, "")

    def test_ready_cards_are_capacity_blocked_once_running_hits_the_cap(self):
        """Row 483: six ready cards, card 7 running, cap 1 - the six show `capacity`, not
        `unowned`. Production-shaped `caps` and a routable `assignee` on every card, proving
        `capacity` is not shadowed by `unassigned` for a card that resolves fine."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        for i in range(1, 7):
            db.new_task(conn, title=f"card {i}", status="ready", branch=EPIC, assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(len(items), 6)    # card 7 excluded: status='running' is not a candidate status
        for item in items:
            self.assertEqual(item.state, "blocked")
            self.assertEqual(item.owner, "")
            self.assertEqual(item.blocker, "capacity: board=my-app cap=1 running=1")

    def test_ready_cards_stay_unowned_when_under_the_cap(self):
        """Routable, under cap, on epic, not busy: genuinely about to dispatch, so omitted."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 1", status="ready", branch=EPIC, assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(items, [])

    def test_no_caps_argument_never_capacity_blocks(self):
        """Backward compatibility: every pre-row-483 caller (and every other test in this
        file) passes no `caps` at all and must see exactly the old behaviour."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running")
        db.new_task(conn, title="card 1", status="ready")    # no branch at all - would be no-epic under caps
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "unblocked")
        self.assertEqual(items[0].owner, "")

    def test_a_card_already_blocked_by_waits_keeps_that_blocker_not_capacity(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        db.new_task(conn, title="waits on a live report", status="ready", branch=EPIC,
                    body="waits: report-commit abc1234\n")
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={"abc1234": ["running"]},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].blocker, "waits: report-commit abc1234")

    def test_review_run_orphan_run_and_expired_claim_never_count_toward_capacity(self):
        """Row 483 fix round 1, I1: none of these three ever sets `tasks.status='running'`, so
        a genuinely free board must never capacity-block the one ready card - had it, the
        routable-assigned card below would show a `capacity:` blocker instead of being omitted."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        # (a) a review run in progress: the card itself stays 'review', only task_runs is 'running'
        review_tid = db.new_task(conn, title="in review", status="review")
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                     (review_tid, "reviewer", "running", 1))
        # (b) an orphan task_runs row: its own task moved on to 'done', the run row never closed
        done_tid = db.new_task(conn, title="long done", status="done")
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?,?,?,?)",
                     (done_tid, "implementer", "running", 1))
        # (c) an expired claim: tasks.status is 'running' but its own claim_expires has lapsed
        expired_tid = db.new_task(conn, title="claim lapsed", status="running")
        conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (500, expired_tid))
        db.new_task(conn, title="should run", status="ready", branch=EPIC, assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(items, [])

    def test_built_card_with_no_live_run_stays_unblocked_at_the_cap(self):
        """Row 483 fix round 1, M1: the capacity override applies only to `ready` cards -
        a `built`/`review` candidate with no live run is unaffected even while at the cap."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        db.new_task(conn, title="stuck in review", status="review")
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "unblocked")
        self.assertEqual(items[0].owner, "")

    def test_ready_card_with_a_bare_current_run_id_is_not_owned_at_the_cap(self):
        """Row 581 (rewrites row 483 fix round 1, M1): a bare `current_run_id` pointer (no
        running `task_runs` row behind it) no longer owns the card, so the cap chain judges it."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        tid = db.new_task(conn, title="mid-workflow", status="ready", branch=EPIC)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", ("run-123", tid))
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].owner, "")

    def test_ready_card_off_the_epic_branch_is_blocked_no_epic(self):
        """Row 483 fix round 1, M2: wrong branch - `no-epic`, never `capacity`/"unowned".
        Routable assignee: `no-epic` isn't shadowed by `unassigned` for a card that resolves."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="off the epic", status="ready", branch="not/an-epic-branch", assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(5, [EPIC]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "blocked")
        self.assertEqual(items[0].blocker, "no-epic")
        self.assertEqual(items[0].owner, "")

    def test_ready_card_with_no_branch_at_all_is_blocked_no_epic(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="no branch set", status="ready", assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(5, [EPIC]))
        self.assertEqual(items[0].blocker, "no-epic")

    def test_no_epic_wins_over_capacity_when_both_would_apply(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        db.new_task(conn, title="off epic and at cap", status="ready", branch="not/an-epic-branch",
                    assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(items[0].blocker, "no-epic")

    def test_ready_card_on_a_busy_branch_is_blocked_dispatcher_hold_branch_busy(self):
        """Row 483 follow-up RR2: under cap, on an epic branch, but the serial-within-branch
        rule (`dispatch.branch_busy`) holds it back. Routable assignee: still applies."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="already running on this branch", status="running", branch=EPIC)
        db.new_task(conn, title="same branch, ready", status="ready", branch=EPIC, assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(2, [EPIC]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "blocked")
        self.assertEqual(items[0].blocker, "dispatcher-hold: branch-busy")
        self.assertEqual(items[0].owner, "")

    def test_overlapping_file_set_on_another_epic_is_not_held(self):
        """One live card per epic clone, not per file set: a running card on a different
        epic never holds this ready card, even with overlapping file sets."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="running on the other epic", status="running", branch="epic/other",
                    file_set=["static/*"])
        db.new_task(conn, title="overlapping file set, different branch", status="ready", branch=EPIC,
                    file_set=["static/timer.html"], assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        items = unblocked.board_items(
            "my-app", capsule_home=home, now=2000,
            run_status_by_commit={},
            caps=caps(2, [EPIC, "epic/other"]))
        self.assertFalse([i for i in items
                          if i.item.endswith("overlapping file set, different branch")])

    def test_ready_card_with_no_assignee_is_blocked_unassigned(self):
        """The gap - a `ready` card no one ever assigned, under cap, on its epic branch,
        otherwise plain unblocked/unowned - is reported `unassigned`, not indistinguishable
        plain `unblocked`."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="no assignee", status="ready", branch=EPIC)
        conn.commit()
        conn.close()
        items = unblocked.board_items(
            "my-app", capsule_home=home, now=2000,
            run_status_by_commit={}, caps=caps(5, [EPIC]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "blocked")
        self.assertEqual(items[0].blocker, "unassigned")
        self.assertEqual(items[0].owner, "")

    def test_ready_card_with_an_unroutable_assignee_is_blocked_unassigned(self):
        """A named assignee that maps to no role, or a role with no routing entry, is exactly
        as stuck as no assignee at all - `dispatch.resolve_assignee` reads both as `None`."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="stale assignee", status="ready", branch=EPIC, assignee="nobody")
        conn.commit()
        conn.close()
        items = unblocked.board_items(
            "my-app", capsule_home=home, now=2000,
            run_status_by_commit={}, caps=caps(5, [EPIC]))
        self.assertEqual(items[0].blocker, "unassigned")

    def test_unassigned_wins_over_capacity_at_the_cap(self):
        """Fix round 1: `unassigned` is checked before `capacity` (and `no-epic`) - on a board
        already at its cap, a permanently-unroutable card must still read `unassigned`, never
        the misleadingly-transient `capacity` (which a card that just needs an assignee would
        never age out of on its own, unlike a card genuinely queued behind the cap)."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        db.new_task(conn, title="no assignee, board at cap", status="ready", branch=EPIC)
        conn.commit()
        conn.close()
        items = unblocked.board_items(
            "my-app", capsule_home=home, now=2000,
            run_status_by_commit={}, caps=caps(1, [EPIC]))
        self.assertEqual(items[0].blocker, "unassigned")

    def test_unassigned_wins_over_no_epic_too(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="off epic, no assignee", status="ready", branch="not/an-epic-branch")
        conn.commit()
        conn.close()
        items = unblocked.board_items(
            "my-app", capsule_home=home, now=2000,
            run_status_by_commit={}, caps=caps(5, [EPIC]))
        self.assertEqual(items[0].blocker, "unassigned")


    def test_board_paused_blocks_a_ready_epic_card_as_board_paused(self):
        """Row 483 fix round 2 (coordinator ruling on the Z6 call): while `pause:board`
        holds, a ready card `dispatch.ready` would otherwise take is reported `blocked`
        with blocker `board-paused` - never plain unblocked/unowned, and checked before
        every other reason. Once the pause lifts, the same card falls back to whichever
        reason applies next (fix round 1: `unassigned`, for a card with no assignee - the
        card here has none, same as before this ruling)."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        db.new_task(conn, title="on epic, paused board", status="ready", branch=EPIC)
        db.guard_set(conn, "pause:board", {"until": 5000, "reason": "rate limit"})
        conn.commit()
        conn.close()
        # Paused (now=2000 < until=5000): board-paused, not unassigned, not unowned.
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "blocked")
        self.assertEqual(items[0].blocker, "board-paused")
        self.assertEqual(items[0].owner, "")
        # Unpaused (now=5000 >= until=5000): falls back to the next reason (unassigned).
        items = unblocked.board_items("my-app", capsule_home=home, now=5000,
                                       run_status_by_commit={},
                                       caps=caps(1, [EPIC]))
        self.assertEqual(items[0].state, "blocked")
        self.assertEqual(items[0].blocker, "unassigned")

    def test_board_paused_wins_over_no_epic_too(self):
        """Board-paused is checked before no-epic (coordinator ruling): a paused board's
        off-epic ready card is `board-paused`, not `no-epic`."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="off epic, paused board", status="ready", branch="not/an-epic-branch")
        db.guard_set(conn, "pause:board", {"until": 5000, "reason": "rate limit"})
        conn.commit()
        conn.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps=caps(5, [EPIC]))
        self.assertEqual(items[0].blocker, "board-paused")

    def test_legacy_shaped_board_skips_caps_checks_with_a_notice_and_never_raises(self):
        """Row 483 follow-up RR1: a board this installation's own tick has not (yet) taken
        over - the old system's own schema only, none of Capsule's additive `guard_state` table
        `dispatch.board_paused` reads - must never make `detect`/`board_items` raise. The
        pause/epic/capacity checks are simply skipped for that board (same reading as no
        `caps` at all), with one `state="notice"` item recording why, never silently."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        (p.parent / "config.json").write_text("{}")
        raw = sqlite3.connect(str(p))
        raw.executescript(board_ddl())    # base schema only - no migrate(), so no guard_state
        raw.execute(
            "INSERT INTO tasks (id, title, status, created_at, branch_name) VALUES (?,?,?,?,?)",
            ("t1", "on epic, pre-handover board", "ready", 1000, EPIC))
        raw.commit()
        raw.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps={"max_running": 1, "epic_branches": [EPIC]})
        notices = [i for i in items if i.state == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].source, "board:my-app")
        cards = [i for i in items if i.state != "notice"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].state, "unblocked")
        self.assertEqual(cards[0].owner, "")
        self.assertEqual(cards[0].blocker, "")

    def test_legacy_shaped_board_with_no_caps_never_gets_a_notice(self):
        """Row 483 follow-up round 1, finding 1: a board `detect` was never asked to apply
        caps to at all (no `caps` argument, the common case for every non-tick-governed
        board) must stay exactly as silent as it was before this follow-up - the first
        `SELECT` no longer names `file_set` at all when `caps is None`, so a legacy-only
        board never gets the "checks skipped" notice for checks it was never going to run
        in the first place."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        (p.parent / "config.json").write_text("{}")
        raw = sqlite3.connect(str(p))
        raw.executescript(board_ddl())
        raw.execute(
            "INSERT INTO tasks (id, title, status, created_at, branch_name) VALUES (?,?,?,?,?)",
            ("t1", "on epic, pre-handover board", "ready", 1000, EPIC))
        raw.commit()
        raw.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual([i for i in items if i.state == "notice"], [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "unblocked")

    def test_a_board_missing_branch_name_too_still_never_raises(self):
        """Row 483 follow-up round 1, finding 3: the fallback read (once the caps-only reads
        have already failed) is built from `PRAGMA table_info`, not a fixed column list - a
        board missing even `branch_name` itself (rarer still than the legacy-only-schema
        case RR1 targets, since that column is part of the old system's own base schema) still
        doesn't raise a second time out of the `except` handler."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        (p.parent / "config.json").write_text("{}")
        raw = sqlite3.connect(str(p))
        raw.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,"
                    " assignee TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL,"
                    " current_run_id INTEGER)")    # no branch_name, no file_set, no guard_state
        raw.execute("CREATE TABLE task_runs (task_id TEXT, status TEXT)")
        raw.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " task_id TEXT, body TEXT, created_at INTEGER)")
        raw.execute("INSERT INTO tasks (id, title, status, created_at) VALUES (?,?,?,?)",
                    ("t1", "no branch_name at all", "ready", 1000))
        raw.commit()
        raw.close()
        items = unblocked.board_items("my-app", capsule_home=home, now=2000,
                                       run_status_by_commit={},
                                       caps={"max_running": 1, "epic_branches": [EPIC]})
        notices = [i for i in items if i.state == "notice"]
        self.assertEqual(len(notices), 1)
        cards = [i for i in items if i.state != "notice"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].state, "unblocked")

    def test_card_body_waits_line_blocks_it(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="waiting on a run", status="ready",
                     body="context\nwaits: report-commit abc1234\n")
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={"abc1234": ["running"]})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "blocked")
        self.assertEqual(items[0].blocker, "waits: report-commit abc1234")

    def test_card_body_waits_owner_is_ignored_and_stays_unblocked(self):
        """Row 470 fix round 1, I3: a card body may declare only report-commit -
        an owner wait written into one is unrecognised there, never a block."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="ask owner", status="ready", body="context\nwaits: owner\n")
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "unblocked")
        self.assertEqual(items[0].blocker, "")

    def test_latest_comment_waits_line_used_when_body_has_none(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="waiting via comment", status="ready", body="plain")
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                     (tid, "x", "waits: report-commit abc1234", 1))
        conn.commit()
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={"abc1234": ["running"]})
        self.assertEqual(items[0].state, "blocked")

    def test_comment_waits_owner_is_ignored_and_stays_unblocked(self):
        """Row 470 fix round 1, I3."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="ask owner via comment", status="ready", body="plain")
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                     (tid, "x", "waits: owner", 1))
        conn.commit()
        conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(items[0].state, "unblocked")

    def test_ready_card_with_a_bare_current_run_id_is_not_owned(self):
        """Row 581 (rewrites row 470 fix round 1, M1): a bare `current_run_id` pointer is a
        leak, not a lease - ownership needs a running `task_runs` row or a fresh heartbeat."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="mid-workflow", status="ready")
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", ("run-123", tid))
        conn.commit(); conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(items[0].owner, "")

    def test_a_fresh_builds_heartbeat_for_the_named_branch_owns_the_card(self):
        """Row 492 follow-up: a `builds: <repo>:<branch>` line in the body, plus a fresh
        `queue/<repo>/owners.json` heartbeat for that branch (`bin/capsule-own`), counts as
        ownership even though the tick's own `current_run_id` was never set."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="built outside the tick", status="ready",
                    body="builds: capsule-corp:capsule/human-assign\n")
        conn.commit()
        conn.close()
        qdir = home / "queue" / "capsule-corp"
        qdir.mkdir(parents=True)
        (qdir / "owners.json").write_text(json.dumps(
            {"capsule/human-assign": {"owner": "implementer", "heartbeat": 2000 - 60}}))
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(items[0].owner, "owners.json:capsule/human-assign")

    def test_a_stale_builds_heartbeat_does_not_own_the_card(self):
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="stale claim", status="ready",
                    body="builds: capsule-corp:capsule/human-assign\n")
        conn.commit()
        conn.close()
        qdir = home / "queue" / "capsule-corp"
        qdir.mkdir(parents=True)
        (qdir / "owners.json").write_text(json.dumps(
            {"capsule/human-assign": {"owner": "implementer", "heartbeat": 2000 - unblocked.OWNER_HEARTBEAT_MAX_S - 1}}))
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(items[0].owner, "")

    def test_a_newer_comment_builds_line_wins_over_an_older_body(self):
        """Same newest-of-body/comment tie-break `waits:` already gets."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="reassigned mid-flight", status="ready",
                          body="builds: capsule-corp:old-branch\n", now=1)
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                     (tid, "x", "builds: capsule-corp:new-branch", 500))
        conn.commit()
        conn.close()
        qdir = home / "queue" / "capsule-corp"
        qdir.mkdir(parents=True)
        (qdir / "owners.json").write_text(json.dumps(
            {"new-branch": {"owner": "implementer", "heartbeat": 2000 - 60},
             "old-branch": {"owner": "implementer", "heartbeat": 2000 - 60}}))
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(items[0].owner, "owners.json:new-branch")

    def test_a_fresh_heartbeat_keyed_by_the_card_id_owns_the_card(self):
        """`bin/capsule-own` accepts a task id as the item name too, not only a branch."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="claimed by card id", status="ready",
                          body="builds: capsule-corp:some-branch\n")
        conn.commit()
        conn.close()
        qdir = home / "queue" / "capsule-corp"
        qdir.mkdir(parents=True)
        (qdir / "owners.json").write_text(json.dumps({tid: {"owner": "implementer", "heartbeat": 2000 - 60}}))
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={})
        self.assertEqual(items[0].owner, "owners.json:card")

    def test_a_newer_comment_wins_over_an_older_body(self):
        """Row 470 fix round 1, M2."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="ask via comment, later", status="ready",
                           body="waits: report-commit def5678\n", now=100)
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                     (tid, "x", "waits: report-commit abc1234", 200))
        conn.commit(); conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={"abc1234": ["running"]})
        self.assertEqual(items[0].blocker, "waits: report-commit abc1234")

    def test_an_older_comment_does_not_override_the_body(self):
        """Row 470 fix round 1, M2."""
        d = Path(tempfile.mkdtemp())
        home = d / "home"
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="body is newer", status="ready",
                           body="waits: report-commit def5678\n", now=500)
        conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                     (tid, "x", "waits: report-commit abc1234", 100))
        conn.commit(); conn.close()
        items = unblocked.board_items("capsule-corp", capsule_home=home, now=2000,
                                       run_status_by_commit={"abc1234": ["running"],
                                                              "def5678": ["running"]})
        self.assertEqual(items[0].blocker, "waits: report-commit def5678")

    def test_missing_board_db_is_silently_empty(self):
        home = Path(tempfile.mkdtemp())
        self.assertEqual(unblocked.board_items("nope", capsule_home=home, now=1,
                                                 run_status_by_commit={}), [])


class LegacyHomeDefault(unittest.TestCase):
    """Row 470 fix round 1, I8 (honest test): the legacy-home helper's own env override,
    kept because other callers (handover, doctor) still use it."""

    def test_env_override_wins(self):
        old = os.environ.get("LEGACY_KANBAN_HOME")
        os.environ["LEGACY_KANBAN_HOME"] = "/tmp/not-a-real-legacy-home-xyz"
        try:
            self.assertEqual(paths.legacy_home(), Path("/tmp/not-a-real-legacy-home-xyz"))
        finally:
            if old is None:
                os.environ.pop("LEGACY_KANBAN_HOME", None)
            else:
                os.environ["LEGACY_KANBAN_HOME"] = old

    def test_default_is_dot_legacy_under_home(self):
        old = os.environ.pop("LEGACY_KANBAN_HOME", None)
        try:
            self.assertEqual(paths.legacy_home(), Path.home() / f".{_LN.NAME}")
        finally:
            if old is not None:
                os.environ["LEGACY_KANBAN_HOME"] = old


class HumanAdapter(unittest.TestCase):
    def test_pending_verb_is_a_candidate_done_ones_are_not(self):
        home = Path(tempfile.mkdtemp())
        d = home / "boards" / "capsule-corp" / "human"
        d.mkdir(parents=True)
        (d / "1.json").write_text(json.dumps({"task_id": "t_x", "kind": "reset", "at": 1000}))
        (d / "done").mkdir()
        (d / "done" / "0.json").write_text(json.dumps({"task_id": "t_y", "kind": "reset", "at": 1}))
        items = unblocked.human_items("capsule-corp", capsule_home=home, now=1000 + 60)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].age_s, 60)

    def test_owner_only_verb_is_blocked(self):
        home = Path(tempfile.mkdtemp())
        d = home / "boards" / "capsule-corp" / "human"
        d.mkdir(parents=True)
        (d / "1.json").write_text(json.dumps({"task_id": "t_x", "kind": "unblock",
                                               "waits": "owner", "at": 1}))
        items = unblocked.human_items("capsule-corp", capsule_home=home, now=2)
        self.assertEqual(items[0].state, "blocked")

    def test_no_human_dir_is_silently_empty(self):
        self.assertEqual(unblocked.human_items("x", capsule_home=Path(tempfile.mkdtemp()), now=1), [])


class RunLayerAdapter(unittest.TestCase):
    def test_done_layer_without_triage_is_a_candidate(self):
        d = Path(tempfile.mkdtemp())
        (d / "progress.json").write_text(json.dumps(
            {"commit": "abc1234", "status": "running",
             "layers": {"l1": {"status": "done"}, "l2": {"status": "running"}}}))
        items = unblocked.run_layer_items([str(d / "progress.json")], now=time.time())
        self.assertEqual([i.item for i in items], [f"{d / 'progress.json'}:l1 (done)"])

    def test_blocked_and_parked_layers_are_also_candidates(self):
        """Row 470 fix round 1, M8: a layer that ran aground in `blocked`/`parked`, not
        only `done`, must still surface."""
        d = Path(tempfile.mkdtemp())
        (d / "progress.json").write_text(json.dumps(
            {"commit": "abc1234", "status": "running",
             "layers": {"l1": {"status": "blocked"}, "l2": {"status": "parked"},
                        "l3": {"status": "running"}}}))
        items = unblocked.run_layer_items([str(d / "progress.json")], now=time.time())
        self.assertEqual({i.item.rsplit(" ", 1)[-1] for i in items}, {"(blocked)", "(parked)"})

    def test_triage_note_clears_a_blocked_layer_too(self):
        d = Path(tempfile.mkdtemp())
        (d / "progress.json").write_text(json.dumps(
            {"commit": "abc1234", "status": "blocked", "layers": {"l1": {"status": "blocked"}}}))
        (d / "triage-l1.md").write_text("looked at it")
        items = unblocked.run_layer_items([str(d / "progress.json")], now=time.time())
        self.assertEqual(items, [])

    def test_triage_note_next_to_it_clears_the_layer(self):
        d = Path(tempfile.mkdtemp())
        (d / "progress.json").write_text(json.dumps(
            {"commit": "abc1234", "status": "done", "layers": {"l1": {"status": "done"}}}))
        (d / "triage-l1.md").write_text("reviewed")
        items = unblocked.run_layer_items([str(d / "progress.json")], now=time.time())
        self.assertEqual(items, [])

    def test_triage_marker_in_the_queue_dir_clears_the_layer(self):
        """Row 483: a run-layer triage marker may live at
        `$CAPSULE_HOME/queue/runs/<run-dir-name>/triage-<layer>.md` instead of beside
        `progress.json` - the alternative for an agent that must never write inside the run's
        own directory."""
        d = Path(tempfile.mkdtemp())
        (d / "progress.json").write_text(json.dumps(
            {"commit": "abc1234", "status": "done", "layers": {"l1": {"status": "done"}}}))
        home = Path(tempfile.mkdtemp())
        marker_dir = home / "queue" / "runs" / d.name
        marker_dir.mkdir(parents=True)
        (marker_dir / "triage-l1.md").write_text("reviewed via the queue dir")
        items = unblocked.run_layer_items([str(d / "progress.json")], now=time.time(), capsule_home=home)
        self.assertEqual(items, [])

    def test_a_different_runs_queue_dir_marker_does_not_clear_this_one(self):
        d = Path(tempfile.mkdtemp())
        (d / "progress.json").write_text(json.dumps(
            {"commit": "abc1234", "status": "done", "layers": {"l1": {"status": "done"}}}))
        home = Path(tempfile.mkdtemp())
        marker_dir = home / "queue" / "runs" / "some-other-run-stamp"
        marker_dir.mkdir(parents=True)
        (marker_dir / "triage-l1.md").write_text("wrong run")
        items = unblocked.run_layer_items([str(d / "progress.json")], now=time.time(), capsule_home=home)
        self.assertEqual(len(items), 1)

    def test_glob_with_no_matches_is_silently_empty(self):
        self.assertEqual(unblocked.run_layer_items(["/no/such/*/progress.json"], now=1), [])

    def test_registry_maps_commit_to_a_list_of_statuses(self):
        d = Path(tempfile.mkdtemp())
        (d / "progress.json").write_text(json.dumps({"commit": "deadbee", "status": "running", "layers": {}}))
        reg = unblocked.run_status_registry([str(d / "progress.json")])
        self.assertEqual(reg, {"deadbee": ["running"]})

    def test_registry_lowercases_and_collects_every_status_for_a_repeated_commit(self):
        """Row 470 fix round 1, I5."""
        d1, d2 = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (d1 / "progress.json").write_text(json.dumps({"commit": "ABC1234", "status": "done", "layers": {}}))
        (d2 / "progress.json").write_text(json.dumps({"commit": "abc1234", "status": "running", "layers": {}}))
        reg = unblocked.run_status_registry([str(d1 / "progress.json"), str(d2 / "progress.json")])
        self.assertEqual(set(reg), {"abc1234"})
        self.assertCountEqual(reg["abc1234"], ["done", "running"])


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def make_repo():
    d = Path(tempfile.mkdtemp())
    git(d, "init", "-q", "-b", "main")
    (d / "f.txt").write_text("1")
    git(d, "add", "f.txt")
    git(d, "commit", "-q", "-m", "init")
    return d


class BranchAdapter(unittest.TestCase):
    def test_no_git_repo_is_silently_empty(self):
        self.assertEqual(unblocked.branch_items(Path(tempfile.mkdtemp()), review_notes=None, now=1,
                                                  run_status_by_commit={}), [])

    def test_ahead_branch_named_fix_star_is_built_unreviewed(self):
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "fix/thing")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        items = unblocked.branch_items(repo, review_notes=None, now=time.time(),
                                        run_status_by_commit={})
        self.assertEqual(len(items), 1)
        self.assertIn("built-unreviewed", items[0].item)
        self.assertEqual(items[0].state, "unblocked")

    def test_merged_branch_is_not_a_candidate(self):
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "fix/merged")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "-m", "merge", "fix/merged")
        items = unblocked.branch_items(repo, review_notes=None, now=time.time(),
                                        run_status_by_commit={})
        self.assertEqual(items, [])

    def test_branch_ahead_with_no_recognised_name_or_marker_is_unclassified(self):
        """Row 470 fix round 1, M5: visible but not a candidate - `state=="unclassified"`,
        which `summarize`/`render_table`'s main count both exclude."""
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "someones-scratch-branch")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        items = unblocked.branch_items(repo, review_notes=None, now=time.time(),
                                        run_status_by_commit={})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "unclassified")
        self.assertEqual(unblocked.summarize(items), (0, 0))

    def test_queue_json_marker_overrides_naming_and_can_set_waits(self):
        """Row 470 fix round 1, I4: the marker lives at
        `$CAPSULE_HOME/queue/<repo>/<branch>.json`, never inside the repo's own tree."""
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "capsule/thing")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        home = Path(tempfile.mkdtemp())
        marker_dir = home / "queue" / repo.name
        marker_dir.mkdir(parents=True)
        (marker_dir / "capsule").mkdir()
        (marker_dir / "capsule" / "thing.json").write_text(
            json.dumps({"state": "needs-review", "waits": "report-commit abc1234"}))
        items = unblocked.branch_items(repo, review_notes=None, now=time.time(),
                                        run_status_by_commit={"abc1234": ["running"]},
                                        capsule_home=home)
        self.assertEqual(len(items), 1)
        self.assertIn("needs-review", items[0].item)
        self.assertEqual(items[0].state, "blocked")
        self.assertEqual(items[0].blocker, "waits: report-commit abc1234")

    def test_queue_json_marker_waits_owner_is_ignored_and_stays_unblocked(self):
        """Row 470 fix round 1, I3: a queue marker may declare only
        report-commit - an owner wait in one is unrecognised, never a block."""
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "capsule/thing2")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        home = Path(tempfile.mkdtemp())
        marker_dir = home / "queue" / repo.name / "capsule"
        marker_dir.mkdir(parents=True)
        (marker_dir / "thing2.json").write_text(
            json.dumps({"state": "needs-review", "waits": "owner"}))
        items = unblocked.branch_items(repo, review_notes=None, now=time.time(),
                                        run_status_by_commit={},
                                        capsule_home=home)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "unblocked")
        self.assertEqual(items[0].blocker, "")

    def test_a_committed_in_tree_capsule_queue_json_is_no_longer_read(self):
        """Row 470 fix round 1, I4: an in-tree `.capsule/queue.json` (the old location) is
        simply invisible now - it must never silently keep working."""
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "capsule/old-style")
        (repo / ".capsule").mkdir()
        marker = {"state": "needs-review", "waits": "other-app"}
        (repo / ".capsule" / "queue.json").write_text(json.dumps(marker))
        git(repo, "add", ".capsule/queue.json")
        git(repo, "commit", "-q", "-m", "marker")
        items = unblocked.branch_items(repo, review_notes=None, now=time.time(),
                                        run_status_by_commit={},
                                        capsule_home=Path(tempfile.mkdtemp()))
        self.assertEqual(len(items), 1)
        self.assertIn("built-unreviewed", items[0].item)    # name-classified, marker not found

    def test_review_note_on_disk_clears_the_branch(self):
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "fix/reviewed-thing")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        notes = Path(tempfile.mkdtemp())
        (notes / f"{repo.name}-fix-reviewed-thing.md").write_text("looked at it")
        items = unblocked.branch_items(repo, review_notes=notes, now=time.time(),
                                        run_status_by_commit={})
        self.assertEqual(items, [])

    def test_fresh_owners_json_heartbeat_marks_it_owned(self):
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "fix/owned-thing")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        home = Path(tempfile.mkdtemp())
        marker_dir = home / "queue" / repo.name
        marker_dir.mkdir(parents=True)
        now = time.time()
        (marker_dir / "owners.json").write_text(json.dumps(
            {"fix/owned-thing": {"owner": "agent-1", "heartbeat": now - 60}}))
        items = unblocked.branch_items(repo, review_notes=None, now=now,
                                        run_status_by_commit={},
                                        capsule_home=home)
        self.assertEqual(items[0].owner, "owners.json")

    def test_stale_owners_json_heartbeat_is_not_owned(self):
        repo = make_repo()
        git(repo, "checkout", "-q", "-b", "fix/stale-thing")
        (repo / "f.txt").write_text("2")
        git(repo, "commit", "-q", "-am", "change")
        home = Path(tempfile.mkdtemp())
        marker_dir = home / "queue" / repo.name
        marker_dir.mkdir(parents=True)
        now = time.time()
        (marker_dir / "owners.json").write_text(json.dumps(
            {"fix/stale-thing": {"owner": "agent-1", "heartbeat": now - 3 * 3600}}))
        items = unblocked.branch_items(repo, review_notes=None, now=now,
                                        run_status_by_commit={},
                                        capsule_home=home)
        self.assertEqual(items[0].owner, "")

    def test_no_main_branch_is_silently_empty(self):
        d = Path(tempfile.mkdtemp())
        git(d, "init", "-q", "-b", "trunk")
        (d / "f.txt").write_text("1")
        git(d, "add", "f.txt")
        git(d, "commit", "-q", "-m", "init")
        self.assertEqual(unblocked.branch_items(d, review_notes=None, now=time.time(),
                                                  run_status_by_commit={}), [])

    def test_80_branches_resolve_in_well_under_a_second(self):
        """Row 470 fix round 1, I1's own fixture: one `for-each-ref` per repo, no
        per-branch git subprocess, keeps this fast regardless of branch count."""
        repo = make_repo()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                               check=True, capture_output=True, text=True, env=env).stdout.strip()
        parent = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                 check=True, capture_output=True, text=True, env=env).stdout.strip()
        for n in range(80):
            # A distinct commit (unique message => unique sha) with no checkout needed -
            # `--no-merged=main` needs branches genuinely ahead, not just extra refs.
            sha = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", tree, "-p", parent, "-m", f"branch {n}"],
                check=True, capture_output=True, text=True, env=env).stdout.strip()
            git(repo, "update-ref", f"refs/heads/fix/branch-{n}", sha)
        start = time.monotonic()
        items = unblocked.branch_items(repo, review_notes=None, now=time.time(),
                                        run_status_by_commit={},
                                        capsule_home=Path(tempfile.mkdtemp()))
        elapsed = time.monotonic() - start
        self.assertEqual(len(items), 80)
        self.assertLess(elapsed, 1.0, f"took {elapsed:.3f}s for 80 branches")


class Config(unittest.TestCase):
    def test_defaults_when_no_config_file(self):
        cfg = unblocked.load_config(Path(tempfile.mkdtemp()))
        self.assertEqual(cfg["boards"], ["capsule-corp"])
        self.assertEqual(cfg["repos"], ["~/CodeBase/capsule-corp"])
        self.assertEqual(cfg["runs"], [])

    def test_unblocked_block_overrides_defaults(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": ["only-one"]}}))
        cfg = unblocked.load_config(home)
        self.assertEqual(cfg["boards"], ["only-one"])
        self.assertEqual(cfg["repos"], ["~/CodeBase/capsule-corp"])

    def test_invalid_json_is_an_environment_error(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text("{not json")
        with self.assertRaises(unblocked.EnvironmentError_):
            unblocked.load_config(home)

    def test_bad_type_in_unblocked_block_is_an_environment_error(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": "not-a-list"}}))
        with self.assertRaises(unblocked.EnvironmentError_):
            unblocked.load_config(home)

    def test_owner_holds_defaults_to_empty(self):
        self.assertEqual(unblocked.load_config(Path(tempfile.mkdtemp()))["owner_holds"], [])

    def test_owner_holds_reads_a_list_of_strings(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({"unblocked": {"owner_holds": ["a", "b"]}}))
        self.assertEqual(unblocked.load_config(home)["owner_holds"], ["a", "b"])

    def test_the_old_holds_key_still_reads_and_the_new_key_wins(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(
            json.dumps({"unblocked": {unblocked.LEGACY_HOLDS_KEY: ["old"]}}))
        self.assertEqual(unblocked.load_config(home)["owner_holds"], ["old"])
        (home / "config.json").write_text(json.dumps({"unblocked": {
            unblocked.LEGACY_HOLDS_KEY: ["old"], "owner_holds": ["new"]}}))
        self.assertEqual(unblocked.load_config(home)["owner_holds"], ["new"])

    def test_the_old_waits_word_still_blocks_like_owner(self):
        self.assertEqual(unblocked.evaluate_blocker(
            human.LEGACY_OWNER, run_status_by_commit={}, allow_owner=True), (True, "waits: owner"))
        self.assertEqual(unblocked.evaluate_blocker(
            human.LEGACY_OWNER, run_status_by_commit={}), (False, ""))


class Summary(unittest.TestCase):
    def test_summarize_counts_unblocked_and_unowned(self):
        items = [
            unblocked.Item("a", "s", "unblocked", owner=""),
            unblocked.Item("b", "s", "unblocked", owner="live-run"),
            unblocked.Item("c", "s", "blocked"),
        ]
        self.assertEqual(unblocked.summarize(items), (2, 1))

    def test_render_table_has_header_rows_and_summary_line(self):
        items = [unblocked.Item("thing", "board:x", "unblocked", age_s=90)]
        out = unblocked.render_table(items)
        self.assertIn("item", out.splitlines()[0])
        self.assertIn("thing", out)
        self.assertIn("UNBLOCKED 1 UNOWNED 1", out)

    def test_render_table_shows_unclassified_under_its_own_heading_uncounted(self):
        """Row 470 fix round 1, M5."""
        items = [unblocked.Item("thing", "board:x", "unblocked", age_s=90),
                 unblocked.Item("my-app:weird-branch (unclassified)", "branch", "unclassified",
                                age_s=10)]
        out = unblocked.render_table(items)
        self.assertIn("UNBLOCKED 1 UNOWNED 1", out)
        self.assertIn("-- unclassified", out)
        self.assertIn("weird-branch", out)
        self.assertNotIn("unclassified", out.split("-- unclassified")[0])

    def test_render_table_shows_capacity_blocked_items_under_their_own_heading(self):
        """Row 483."""
        items = [unblocked.Item("my-app:t_1 card one", "board:my-app", "blocked",
                                 blocker="capacity: board=my-app cap=1 running=1")]
        out = unblocked.render_table(items)
        self.assertIn("UNBLOCKED 0 UNOWNED 0", out)
        self.assertIn("-- capacity", out)
        self.assertIn("card one", out.split("-- capacity")[1])
        # still shown (and counted as blocked, not unowned) in the main table too, unlike unclassified
        self.assertIn("card one", out.split("-- capacity")[0])

    def test_render_table_shows_no_epic_blocked_items_under_their_own_heading(self):
        """Row 483 fix round 1, M2."""
        items = [unblocked.Item("my-app:t_2 card two", "board:my-app", "blocked",
                                blocker="no-epic")]
        out = unblocked.render_table(items)
        self.assertIn("-- not dispatchable (no epic branch)", out)
        self.assertIn("card two", out.split("-- not dispatchable")[1])
        self.assertIn("card two", out.split("-- not dispatchable")[0])

    def test_render_table_shows_board_paused_items_under_their_own_heading(self):
        """Row 483 fix round 2 (coordinator ruling)."""
        items = [unblocked.Item("my-app:t_3 card three", "board:my-app", "blocked",
                                blocker="board-paused")]
        out = unblocked.render_table(items)
        self.assertIn("-- board paused --", out)
        self.assertIn("card three", out.split("-- board paused --")[1])
        self.assertIn("card three", out.split("-- board paused --")[0])

    def test_render_table_shows_dispatcher_hold_items_under_their_own_heading(self):
        """Row 483 follow-up RR2 (round 1 finding 6)."""
        items = [unblocked.Item("my-app:t_4 card four", "board:my-app", "blocked",
                                 blocker="dispatcher-hold: branch-busy")]
        out = unblocked.render_table(items)
        self.assertIn("-- dispatcher hold", out)
        self.assertIn("card four", out.split("-- dispatcher hold")[1])
        # still shown (and counted as blocked, not unowned) in the main table too
        self.assertIn("card four", out.split("-- dispatcher hold")[0])

    def test_render_json_round_trips(self):
        items = [unblocked.Item("thing", "board:x", "unblocked")]
        doc = json.loads(unblocked.render_json(items))
        self.assertEqual((doc["unblocked"], doc["unowned"]), (1, 1))
        self.assertEqual(doc["items"][0]["item"], "thing")


class Main(unittest.TestCase):
    def test_exit_0_when_nothing_unowned(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": [], "repos": []}}))
        import io
        buf = io.StringIO()
        rc = unblocked.main([], capsule_home=home, now=1, stdout=buf)
        self.assertEqual(rc, 0)
        self.assertIn("UNBLOCKED 0 UNOWNED 0", buf.getvalue())

    def test_exit_1_when_something_unowned(self):
        home = Path(tempfile.mkdtemp())
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="do it", status="ready")
        conn.close()
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": ["capsule-corp"], "repos": []}}))
        import io
        buf = io.StringIO()
        rc = unblocked.main([], capsule_home=home, now=1, stdout=buf)
        self.assertEqual(rc, 1)

    def test_exit_2_on_environment_error(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text("{not json")
        import io
        buf = io.StringIO()
        rc = unblocked.main([], capsule_home=home, now=1, stdout=buf)
        self.assertEqual(rc, 2)

    def test_exit_2_on_any_detector_error_not_only_environmenterror(self):
        """Row 470 fix round 1, I7: an unexpected exception from a source (a locked db, a
        permission error) must be exit 2, not an uncaught traceback."""
        home = Path(tempfile.mkdtemp())
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        p.chmod(0o000)
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": ["capsule-corp"], "repos": []}}))
        import io
        buf = io.StringIO()
        try:
            rc = unblocked.main([], capsule_home=home, now=1, stdout=buf)
        finally:
            p.chmod(0o644)
        self.assertEqual(rc, 2)

    def test_json_flag_prints_json(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": [], "repos": []}}))
        import io
        buf = io.StringIO()
        unblocked.main(["--json"], capsule_home=home, now=1, stdout=buf)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["unblocked"], 0)


class Detect(unittest.TestCase):
    def test_owner_holds_forces_a_named_item_blocked(self):
        """Row 470 fix round 1, I3: the *other* allowed source of an owner block."""
        home = Path(tempfile.mkdtemp())
        p = home / "boards" / "capsule-corp" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        tid = db.new_task(conn, title="needs a human call", status="ready")
        conn.close()
        held_item = f"capsule-corp:{tid} needs a human call"
        (home / "config.json").write_text(json.dumps({"unblocked": {
            "boards": ["capsule-corp"], "repos": [], "owner_holds": [held_item]}}))
        items = unblocked.detect(capsule_home=home, now=1000)
        item = next(i for i in items if i.item == held_item)
        self.assertEqual(item.state, "blocked")
        self.assertEqual(item.blocker, "waits: owner")

    def test_detect_merges_every_source_and_shares_the_commit_registry(self):
        home = Path(tempfile.mkdtemp())
        board_db = home / "boards" / "capsule-corp" / "kanban.db"
        board_db.parent.mkdir(parents=True)
        _create_board(board_db)
        conn = db.connect(board_db)
        db.new_task(conn, title="waits on a live report", status="ready",
                     body="waits: report-commit cafef00d\n")
        conn.close()
        runs_dir = Path(tempfile.mkdtemp())
        (runs_dir / "progress.json").write_text(json.dumps(
            {"commit": "cafef00d", "status": "running", "layers": {}}))
        (home / "config.json").write_text(json.dumps({"unblocked": {
            "boards": ["capsule-corp"], "repos": [], "runs": [str(runs_dir / "progress.json")]}}))
        items = unblocked.detect(capsule_home=home, now=1000)
        board_item = next(i for i in items if i.source == "board:capsule-corp")
        self.assertEqual(board_item.state, "blocked")
        self.assertEqual(board_item.blocker, "waits: report-commit cafef00d")


    def test_ready_cards_beyond_the_cap_show_unassigned_not_unowned(self):
        """Row 483 acceptance case, end to end through `detect`: card 7 running, cap 1, six
        unassigned ready cards on `my-app` - none of them counts as unowned any more, and
        (fix round 1) each reads its own actionable `unassigned` rather than the coarser,
        misleadingly-transient `capacity` a card with no routable assignee would never clear
        on its own anyway."""
        home = Path(tempfile.mkdtemp())
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        for i in range(1, 7):
            db.new_task(conn, title=f"card {i}", status="ready", branch=EPIC)
        conn.commit()
        conn.close()
        (home / "config.json").write_text(json.dumps({
            "board": "my-app", "caps": {"max_running": 1}, "epics": [EPIC], **ROUTING,
            "unblocked": {"boards": ["my-app"], "repos": []}}))
        items = unblocked.detect(capsule_home=home, now=2000,
                                 dispatcher_check=lambda board, home: "live")
        board_items = [i for i in items if i.source == "board:my-app"]
        self.assertEqual(len(board_items), 6)
        for item in board_items:
            self.assertEqual(item.state, "blocked")
            self.assertEqual(item.blocker, "unassigned")
        n, m = unblocked.summarize(items)
        self.assertEqual(m, 0)

    def test_ready_cards_with_routable_assignees_beyond_the_cap_show_capacity(self):
        """Same acceptance case, end to end through `detect`, with routable assignees: `capacity`
        still applies, not shadowed by `unassigned`."""
        home = Path(tempfile.mkdtemp())
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        for i in range(1, 7):
            db.new_task(conn, title=f"card {i}", status="ready", branch=EPIC, assignee=ASSIGNEE)
        conn.commit()
        conn.close()
        (home / "config.json").write_text(json.dumps({
            "board": "my-app", "caps": {"max_running": 1}, "epics": [EPIC], **ROUTING,
            "unblocked": {"boards": ["my-app"], "repos": []}}))
        items = unblocked.detect(capsule_home=home, now=2000,
                                 dispatcher_check=lambda board, home: "live")
        board_items = [i for i in items if i.source == "board:my-app"]
        self.assertEqual(len(board_items), 6)
        for item in board_items:
            self.assertEqual(item.state, "blocked")
            self.assertTrue(item.blocker.startswith("capacity:"), item.blocker)
        n, m = unblocked.summarize(items)
        self.assertEqual(m, 0)

    def test_legacy_shaped_board_never_raises_through_detect(self):
        """Row 483 follow-up RR1, end to end through `detect` (second oracle beside the
        `board_items`-level unit test above): a board with none of Capsule's own
        `guard_state`/handover state must not blow up the whole run."""
        home = Path(tempfile.mkdtemp())
        p = home / "boards" / "my-app" / "kanban.db"
        p.parent.mkdir(parents=True)
        (p.parent / "config.json").write_text("{}")
        raw = sqlite3.connect(str(p))
        raw.executescript(board_ddl())
        raw.execute(
            "INSERT INTO tasks (id, title, status, created_at, branch_name) VALUES (?,?,?,?,?)",
            ("t1", "on epic, pre-handover board", "ready", 1000, EPIC))
        raw.commit()
        raw.close()
        (home / "config.json").write_text(json.dumps({
            "board": "my-app", "caps": {"max_running": 1}, "epics": [EPIC],
            "unblocked": {"boards": ["my-app"], "repos": []}}))
        items = unblocked.detect(capsule_home=home, now=2000,
                                 dispatcher_check=lambda board, home: "live")
        board_items_ = [i for i in items if i.source == "board:my-app"]
        self.assertEqual(any(i.state == "notice" for i in board_items_), True)
        card = next(i for i in board_items_ if i.state != "notice")
        self.assertEqual(card.state, "unblocked")
        self.assertEqual(card.owner, "")

    def test_caps_only_applies_to_the_configured_board(self):
        """Row 483: `my-app`'s cap must never leak onto `capsule-corp`'s own ready cards.
        Row 483 fix round 1, I3: `capsule-corp` itself is put AT the same cap (its own
        `tasks.status='running'` count == `max_running`) - a leaked cap-application (e.g.
        `caps = caps_cfg` for every board, not only the configured one) would otherwise
        still show this ready card `unblocked` by sheer accident (0 running < any cap), a
        vacuous test. With a genuinely-at-cap capsule-corp board, only the correct
        per-board scoping keeps its ready card unblocked."""
        home = Path(tempfile.mkdtemp())
        app_db = home / "boards" / "my-app" / "kanban.db"
        cc_db = home / "boards" / "capsule-corp" / "kanban.db"
        app_db.parent.mkdir(parents=True)
        cc_db.parent.mkdir(parents=True)
        _create_board(app_db)
        _create_board(cc_db)
        conn = db.connect(app_db)
        db.new_task(conn, title="card 7", status="running", branch=EPIC)
        conn.commit()
        conn.close()
        conn = db.connect(cc_db)
        db.new_task(conn, title="capsule-corp running card", status="running")
        db.new_task(conn, title="capsule-corp ready card", status="ready")
        conn.commit()
        conn.close()
        (home / "config.json").write_text(json.dumps({
            "board": "my-app", "caps": {"max_running": 1}, "epics": [EPIC],
            "unblocked": {"boards": ["my-app", "capsule-corp"], "repos": []}}))
        items = unblocked.detect(capsule_home=home, now=2000,
                                 dispatcher_check=lambda board, home: "live")
        cc_item = next(i for i in items if i.source == "board:capsule-corp")
        self.assertEqual(cc_item.state, "unblocked")
        self.assertEqual(cc_item.owner, "")


class DispatcherBlockerFn(unittest.TestCase):
    """`unblocked.dispatcher_blocker` directly - a standalone oracle; Task 11 drops `legacy`."""
    def test_every_state(self):
        for s, want in (("live", None), ("none", (False, "no-dispatcher")),
                         ("uncertain", (True, "dispatcher-uncertain"))):
            self.assertEqual(unblocked.dispatcher_blocker(s), want, s)


class IntentExamples(unittest.TestCase):
    """Row 470 fix round 1, I8 (honest test): every row 454 intent example runs through the
    oracle its own document identifies, not merely linted, and must match `expect`."""

    def _my_app_board(self, assignee=None, body="", legacy_shaped=False):
        """A throwaway `my-app` board, one `ready` card on `epic/x` (Z7: shared, was x5)."""
        home = Path(tempfile.mkdtemp())
        board_dir = home / "boards" / "my-app"
        board_dir.mkdir(parents=True)
        p = board_dir / "kanban.db"
        if legacy_shaped:
            (p.parent / "config.json").write_text("{}")
            raw = sqlite3.connect(str(p))
            raw.executescript(board_ddl())
            raw.execute("INSERT INTO tasks (id, title, status, created_at, branch_name)"
                        " VALUES ('t1','x','ready',1000,'epic/x')")
            raw.commit(); raw.close()
            return home
        _create_board(p)
        conn = db.connect(p)
        db.new_task(conn, title="x", status="ready", branch="epic/x", assignee=assignee, body=body)
        conn.commit(); conn.close()
        return home

    def test_assigned_waits_blocked_card_is_reported_when_caps_is_set(self):
        """`caps` set: an assigned ready card held by `waits:` is reported `blocked`, not
        omitted as with `caps=None`; it is not unowned-idle work. Its intent example ships."""
        shipped = (paths.INTENT / "unblocked.jsonl").read_text()
        self.assertIn("assigned-waits-blocked-ready-card-is-reported-with-caps", shipped)
        home = self._my_app_board(assignee=ASSIGNEE, body="waits: report-commit abc1234\n")
        items = unblocked.board_items(
            "my-app", capsule_home=home, now=2000,
            run_status_by_commit={"abc1234": ["running"]}, caps=caps(5, ["epic/x"]))
        self.assertEqual([(i.state, i.blocker) for i in items],
                         [("blocked", "waits: report-commit abc1234")])
        self.assertEqual(unblocked.summarize(items), (0, 0))

    def test_assigned_owned_ready_card_is_reported_when_caps_is_set(self):
        """`caps` set: an assigned ready card with a live run is reported with its owner, not
        omitted, and it does not count as unowned-idle work."""
        home = self._my_app_board(assignee=ASSIGNEE)
        conn = db.connect(home / "boards" / "my-app" / "kanban.db")
        tid = conn.execute("SELECT id FROM tasks").fetchone()["id"]
        conn.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?,?,?)",
                     (tid, "running", 1))
        conn.commit()
        conn.close()
        items = unblocked.board_items(
            "my-app", capsule_home=home, now=2000,
            run_status_by_commit={}, caps=caps(5, ["epic/x"]))
        self.assertEqual([(i.state, i.owner) for i in items], [("unblocked", "live-run")])
        self.assertEqual(unblocked.summarize(items), (1, 0))

    def test_c25_row_ships_and_its_oracle_holds(self):
        self.assertTrue(c25_example_holds())

    def test_done_branch_examples_are_pinned(self):
        lines = (paths.INTENT / "unblocked.jsonl").read_text().splitlines()
        ids = {json.loads(x)["id"] for x in lines}
        self.assertLessEqual({"landed-branch-is-cleanup-not-counted-work",
                              "built-unreviewed-branch-is-counted-work",
                              "landed-branch-with-commits-after-marker-is-counted-work"}, ids)

    @staticmethod
    def _simple_blocked(doc):
        if "cap_max_running" in doc:
            return unblocked.capacity_blocked(doc["running_count"], doc["cap_max_running"])
        if "branch_in_epics" in doc:
            epics = ["epic/x"] if doc["branch_in_epics"] else []
            return not unblocked.epic_branch_ok("epic/x", epics)
        if "board_paused" in doc:
            return bool(doc["board_paused"])
        return None

    def _assigned_caps_row_holds(self, row, doc):
        """A `waits:` row through `board_items` with `caps` set (an optional live run)."""
        home = self._my_app_board(assignee=ASSIGNEE, body=f"waits: {doc['waits']}\n")
        if doc.get("live_run"):
            conn = db.connect(home / "boards" / "my-app" / "kanban.db")
            conn.execute("INSERT INTO task_runs (task_id, status, started_at)"
                         " SELECT id, 'running', 1 FROM tasks")
            conn.commit()
            conn.close()
        items = unblocked.board_items(
            "my-app", capsule_home=home,
            now=2000, caps=caps(5, ["epic/x"]),
            run_status_by_commit={"abc1234": ["running"]})
        got = bool(items) and items[0].state == "blocked"
        self.assertEqual(got, row["expect"]["blocked"], row["id"])
        self.assertEqual(items[0].blocker if items else "", row["expect"]["blocker"], row["id"])
        if "state" in row["expect"]:    # reported (not omitted) only with a live run
            self.assertEqual((items[0].state, items[0].owner),
                             (row["expect"]["state"], row["expect"]["owner"]), row["id"])

    def test_every_example_matches_its_oracle(self):
        lines = (paths.INTENT / "unblocked.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        self.assertIn("landed-branch-with-only-an-idle-sync-merge-is-not-counted",
                      {json.loads(ln)["id"] for ln in lines})    # t_1c84a9be: the example must ship
        for line in lines:
            row = json.loads(line)
            doc = row["given"]["document"]
            if "dispatcher" in doc:    # fix round 1 F1: through board_items, assigned (row 496(9))
                items = unblocked.board_items(
                    "my-app", capsule_home=self._my_app_board(assignee=ASSIGNEE),
                    now=2000, run_status_by_commit={},
                    caps=caps(5, ["epic/x"], dispatcher=doc["dispatcher"]))
                self.assertEqual(items[0].state == "blocked", row["expect"]["blocked"], row["id"])
                continue
            if (simple := self._simple_blocked(doc)) is not None:    # separately derived
                self.assertEqual(simple, row["expect"]["blocked"], row["id"])
                continue
            if {"capsule_config_json", "legacy_top_level_board"} & set(doc):    # C25, D244
                self.assertTrue(c25_example_holds(row), row["id"])
                continue
            if doc.get("board_schema_without_capsule_tables"):    # row 483 RR1: no Capsule tables
                items = unblocked.board_items(
                    "my-app", capsule_home=self._my_app_board(legacy_shaped=True),
                    now=2000, run_status_by_commit={},
                    caps={"max_running": 1, "epic_branches": ["epic/x"]})
                card = next(i for i in items if i.state != "notice")
                self.assertEqual(card.state == "blocked", row["expect"]["blocked"], row["id"])
                continue
            if "marker_state" in doc:    # landed/superseded: listed, never counted
                from capsule.tests.test_unblocked_done import detect, fixture
                home = fixture({"a": doc["marker_state"]})
                if doc.get("only_new_commit_is_merge_of_older_main"):
                    from capsule.tests.test_unblocked_done import idle_sync_then_main_moves
                    idle_sync_then_main_moves(home, "a")
                if doc.get("commits_after_marker"):
                    os.utime(next(home.glob("queue/*/capsule/a.json")), (1000, 1000))
                counted = unblocked.summarize(detect(home))[0] > 0
                self.assertEqual(counted, row["expect"]["counted"], row["id"])
                continue
            if "dispatcher_hold_branch_busy" in doc:    # row 483 follow-up RR2: likewise, not a `waits:` rule
                p = Path(tempfile.mkdtemp()) / "k.db"
                _create_board(p)
                conn = db.connect(p); self.addCleanup(conn.close)
                busy = {"epic/x"} if doc["dispatcher_hold_branch_busy"] else set()
                card = {"branch_name": "epic/x", "file_set": None, "id": "t1"}
                reason = unblocked.dispatcher_hold_reason(conn, card, busy)
                self.assertEqual(reason is not None, row["expect"]["blocked"], row["id"])
                continue
            if "assigned_caps_set" in doc:    # real, through `board_items` with `caps` set
                self._assigned_caps_row_holds(row, doc)
                continue
            if "assignee_routable" in doc:    # row 492: real, through `board_items`'s own caps chain
                assignee = ASSIGNEE if doc["assignee_routable"] else None
                items = unblocked.board_items(
                    "my-app", capsule_home=self._my_app_board(assignee=assignee),
                    now=2000, run_status_by_commit={}, caps=caps(5, ["epic/x"]))
                self.assertEqual(items[0].state == "blocked", row["expect"]["blocked"], row["id"])
                continue
            if "builds_owner_heartbeat_fresh" in doc:    # row 492 follow-up: a real owners.json file
                home = self._my_app_board(body="builds: my-app:some-branch\n")
                qdir = home / "queue" / "my-app"
                qdir.mkdir(parents=True)
                fresh = doc["builds_owner_heartbeat_fresh"]
                heartbeat = 2000 - 60 if fresh else 2000 - unblocked.OWNER_HEARTBEAT_MAX_S - 1
                (qdir / "owners.json").write_text(json.dumps(
                    {"some-branch": {"owner": "x", "heartbeat": heartbeat}}))
                items = unblocked.board_items(
                    "my-app", capsule_home=home, now=2000,
                    run_status_by_commit={})
                self.assertEqual(bool(items[0].owner), row["expect"]["owned"], row["id"])
                continue
            waits = None if doc.get("waits") in (None, "<empty>") else doc["waits"]
            run_status_by_commit = {}
            if doc.get("report_commit_run_status") and waits and waits.startswith("report-commit"):
                sha = waits.split(None, 1)[1]
                run_status_by_commit = {sha.lower(): [doc["report_commit_run_status"]]}
            allow_owner = doc.get("source") == "human"
            blocked, _ = unblocked.evaluate_blocker(
                waits, run_status_by_commit=run_status_by_commit,
                allow_owner=allow_owner)
            self.assertEqual(blocked, row["expect"]["blocked"], row["id"])


if __name__ == "__main__":
    unittest.main()
