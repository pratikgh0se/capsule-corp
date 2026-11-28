"""Card t_e905062b: `epic_sync.after_gate_pass` merges `origin/main` into an epic's clone right
after a card's gate pass, so the epic branch never drifts off main between cards. Two oracles
per case: the function's own return value/event (one oracle) plus the clone's real git state -
`git log`/`git status` read independently of `epic_sync` (the second oracle, Z2). `EpicSyncHook`
below is the tick-path oracle (the hook fires exactly once after a gate pass, never on a failed
round, and a real conflict is reachable end to end through the real `Tick`)."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, epic_clean, epic_sync, paths, tick_runs, workspace
from capsule.tests import tmpclean
from capsule.tests.test_tick import BRANCH as TICK_BRANCH
from capsule.tests.test_tick import IMPL_DONE, VAL_FAIL_ONE, TickCase
from capsule.tests.test_tick import git as tick_git

BRANCH = "epic/my-app"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout


class EpicSyncCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.origin = self.dir / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "README.md").write_text("main\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(self.origin, "branch", BRANCH)
        self.root = self.dir / "root"
        self.ws = workspace.clone(str(self.origin), BRANCH, self.root)
        self.board = self.dir / "board.db"
        db.create_board(self.board)
        self.conn = db.connect(self.board)
        self.addCleanup(self.conn.close)
        self.cfg = {"epics": [BRANCH]}

    def new_task(self, branch=BRANCH):
        tid = db.new_task(self.conn, title="t", assignee="dev", branch=branch, status="review")
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


class CleanMerge(EpicSyncCase):
    def test_a_clean_merge_of_origin_main_advances_the_clone_and_records_the_sha(self):
        (self.origin / "elsewhere.txt").write_text("new on main\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved on")
        origin_main = git(self.origin, "rev-parse", "main").strip()
        before = workspace.head(self.ws)
        task = self.new_task()
        ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, self.ws, now=1)
        self.assertTrue(ok)
        after = workspace.head(self.ws)
        self.assertNotEqual(after, before)
        parents = workspace._git("log", "-1", "--format=%P", ws=self.ws).stdout.split()
        self.assertIn(origin_main, parents)
        self.assertIn(before, parents)
        row = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='epic_synced'",
                                (task["id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertIn(after, row["payload"])
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()[0],
                         "review")   # untouched: _verdict marks it done, not this call

    def test_nothing_to_merge_is_a_clean_no_op(self):
        """A no-op merge (main has nothing new) is not a sync - no `epic_synced` event."""
        before = workspace.head(self.ws)
        task = self.new_task()
        ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, self.ws, now=1)
        self.assertTrue(ok)
        self.assertEqual(workspace.head(self.ws), before)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='epic_synced'", (task["id"],)).fetchone())

    def test_a_branch_that_is_not_a_declared_epic_is_left_alone(self):
        task = self.new_task(branch="not-an-epic")
        before = workspace.head(self.ws)
        ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, self.ws, now=1)
        self.assertTrue(ok)
        self.assertEqual(workspace.head(self.ws), before)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='epic_synced'", (task["id"],)).fetchone())


class Conflict(EpicSyncCase):
    def test_a_conflicting_merge_aborts_leaves_the_clone_clean_and_blocks_the_card(self):
        (self.origin / "README.md").write_text("main changed it\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main edits README")
        (self.ws / "README.md").write_text("the card changed it too\n")
        workspace._git(*workspace.IDENTITY, "add", "-A", ws=self.ws)
        workspace._git(*workspace.IDENTITY, "commit", "-qm", "card work", ws=self.ws)
        before = workspace.head(self.ws)
        task = self.new_task()
        ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, self.ws, run_id="r1", now=1)
        self.assertFalse(ok)
        self.assertEqual(workspace.head(self.ws), before)   # merge aborted, nothing new on HEAD
        self.assertEqual(workspace._git("status", "--porcelain", ws=self.ws).stdout.strip(), "")
        self.assertFalse((workspace.gitdir(self.ws) / "MERGE_HEAD").exists())
        row = self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["block_kind"], epic_sync.CONFLICT_KIND)
        self.assertEqual(self.conn.execute("SELECT failure_count FROM tasks WHERE id=?",
                                           (task["id"],)).fetchone()[0], 0)   # no strike
        payload = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",
                                    (task["id"],)).fetchone()["payload"]
        self.assertIn("README.md", payload)


class MergeFailureWithoutConflict(EpicSyncCase):
    def test_unrelated_histories_holds_epic_sync_error_never_runs_merge_abort(self):
        """A merge failure with no `MERGE_HEAD` (unrelated histories, here; also a refused
        merge or a timeout) must not run `merge --abort` (git itself refuses that: "no merge to
        abort") and must not let the exception escape `after_gate_pass` after the card's commit -
        it holds the card instead, clone untouched."""
        git(self.origin, "checkout", "-q", "--orphan", "tmp-unrelated")
        git(self.origin, "rm", "-q", "-rf", ".")
        (self.origin / "OTHER.md").write_text("unrelated root\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "unrelated root")
        git(self.origin, "branch", "-D", "main")
        git(self.origin, "branch", "-m", "tmp-unrelated", "main")
        before = workspace.head(self.ws)
        task = self.new_task()
        ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, self.ws, run_id="r1", now=1)
        self.assertFalse(ok)
        self.assertEqual(workspace.head(self.ws), before)
        self.assertEqual(workspace._git("status", "--porcelain", ws=self.ws).stdout.strip(), "")
        self.assertFalse((workspace.gitdir(self.ws) / "MERGE_HEAD").exists())
        row = self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["block_kind"], epic_sync.ERROR_KIND)


class FetchFailure(EpicSyncCase):
    def test_an_unreachable_origin_records_epic_sync_skipped_and_holds_transient(self):
        """Only a missing `origin/main` ref is nothing-to-sync; any other fetch failure
        (network, auth, the remote gone) must not mark the card done unsynced and silent."""
        shutil.rmtree(self.origin)
        before = workspace.head(self.ws)
        task = self.new_task()
        ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, self.ws, run_id="r1", now=1)
        self.assertFalse(ok)
        self.assertEqual(workspace.head(self.ws), before)
        row = self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["block_kind"], "transient")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='epic_sync_skipped'", (task["id"],)).fetchone())


class Dirty(EpicSyncCase):
    def test_a_dirty_clone_is_reported_and_never_reset(self):
        """Row 508: the same rule `sync_to_origin` follows for a preflight dirty clone, including
        the same `worktree_mismatch` event the row-508 preflight hold records."""
        (self.ws / "README.md").write_text("uncommitted\n")
        task = self.new_task()
        before = workspace.head(self.ws)
        ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, self.ws, now=1)
        self.assertFalse(ok)
        self.assertEqual(workspace.head(self.ws), before)
        self.assertEqual((self.ws / "README.md").read_text(), "uncommitted\n")   # never reset/checked out
        row = self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["block_kind"], "needs_input")
        payload = self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='worktree_mismatch'",
            (task["id"],)).fetchone()
        self.assertIsNotNone(payload)
        self.assertIn("worktree_dirty", payload["payload"])


def _move_host(origin):
    """Commit on the host epic branch so a push from the clone is no longer a fast-forward."""
    git(origin, "checkout", "-q", BRANCH)
    (origin / "other.txt").write_text("someone else\n")
    git(origin, "add", ".")
    git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "moved")
    git(origin, "checkout", "-q", "main")


class Publish(EpicSyncCase):
    """t_820cbe78: `_publish` fast-forwards the host epic branch from the clone."""

    def host_tip(self):
        return git(self.origin, "rev-parse", f"refs/heads/{BRANCH}").strip()

    def card_commit(self):
        (self.ws / "card.txt").write_text("work\n")
        return workspace.commit(self.ws, "card")

    def test_a_gate_passed_commit_is_on_the_host_epic_branch(self):
        sha = self.card_commit()
        self.assertNotEqual(self.host_tip(), sha)
        task = self.new_task()
        self.assertTrue(epic_sync._publish(self.conn, self.cfg, task, self.ws, now=1))
        self.assertEqual(self.host_tip(), sha)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='epic_published'",
            (task["id"],)).fetchone())

    def test_a_non_ff_push_is_refused_with_an_event_and_a_hold(self):
        sha = self.card_commit()
        _move_host(self.origin)
        moved = self.host_tip()
        task = self.new_task()
        self.assertFalse(
            epic_sync._publish(self.conn, self.cfg, task, self.ws, run_id="r1", now=1))
        self.assertEqual(self.host_tip(), moved)         # never forced
        self.assertNotEqual(moved, sha)
        row = self.conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", epic_sync.ERROR_KIND))
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='epic_push_refused'",
            (task["id"],)).fetchone())

    def host_worktree(self):
        wt = self.dir / "host-wt"
        git(self.origin, "worktree", "add", "-q", str(wt), BRANCH)
        return wt

    def test_a_clean_host_worktree_on_the_branch_is_detached_and_the_push_proceeds(self):
        sha = self.card_commit()
        wt = self.host_worktree()
        task = self.new_task()
        self.assertTrue(epic_sync._publish(self.conn, self.cfg, task, self.ws, now=1))
        self.assertEqual(self.host_tip(), sha)
        self.assertEqual(git(wt, "branch", "--show-current").strip(), "")

    def test_a_clean_host_worktree_that_is_ahead_is_left_on_the_branch(self):
        self.card_commit()
        wt = self.host_worktree()
        git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
            "-m", "ahead")
        task = self.new_task()
        self.assertFalse(epic_sync._publish(self.conn, self.cfg, task, self.ws, now=1))
        self.assertEqual(git(wt, "branch", "--show-current").strip(), BRANCH)

    def test_a_dirty_host_worktree_on_the_branch_holds_the_card_and_names_it(self):
        sha = self.card_commit()
        wt = self.host_worktree()
        (wt / "README.md").write_text("uncommitted\n")
        task = self.new_task()
        self.assertFalse(epic_sync._publish(self.conn, self.cfg, task, self.ws, now=1))
        self.assertNotEqual(self.host_tip(), sha)
        self.assertEqual(git(wt, "branch", "--show-current").strip(), BRANCH)
        row = self.conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", epic_sync.ERROR_KIND))
        event = self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",
            (task["id"],)).fetchone()
        self.assertIn(str(wt), event["payload"])

    def test_a_host_worktree_whose_directory_is_gone_does_not_crash_publish(self):
        self.card_commit()
        wt = self.host_worktree()
        shutil.rmtree(wt)
        task = self.new_task()
        self.assertIsInstance(epic_sync._publish(self.conn, self.cfg, task, self.ws, now=1), bool)

    def test_a_branch_that_is_not_a_declared_epic_is_not_pushed(self):
        sha = self.card_commit()
        task = self.new_task(branch="other")
        self.assertTrue(epic_sync._publish(self.conn, self.cfg, task, self.ws, now=1))
        self.assertNotEqual(self.host_tip(), sha)


class IntentExamples(unittest.TestCase):
    """Every `contracts/intent/epic_sync.jsonl` example, run through the real check
    (`after_gate_pass`) against a git fixture built from its `given`, not merely linted -
    `test_workspace.PreflightIntentExamples` follows the same shape for its sibling family."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.board = self.dir / "board.db"
        db.create_board(self.board)
        self.conn = db.connect(self.board)
        self.addCleanup(self.conn.close)
        self.cfg = {"epics": [BRANCH]}

    def _fixture(self, doc):
        n = len(list(self.dir.glob("origin-*")))
        origin = self.dir / f"origin-{n}"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        (origin / "README.md").write_text("main\n")
        git(origin, "add", ".")
        git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(origin, "branch", BRANCH)
        ws = workspace.clone(str(origin), BRANCH, self.dir / f"root-{n}")
        if not doc.get("nothing_new_on_main"):
            if doc.get("merge_would_conflict"):
                (origin / "README.md").write_text("main changed it\n")
            else:
                (origin / "elsewhere.txt").write_text("main moved\n")
            git(origin, "add", ".")
            git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved")
        if doc.get("merge_would_conflict"):
            (ws / "README.md").write_text("the card changed it too\n")
            workspace._git(*workspace.IDENTITY, "add", "-A", ws=ws)
            workspace._git(*workspace.IDENTITY, "commit", "-qm", "card work", ws=ws)
        if doc.get("clone_is_dirty"):
            (ws / "README.md").write_text("uncommitted\n")
        if doc.get("check") == "epic_publish":
            (ws / "card.txt").write_text("card work\n")
            workspace.commit(ws, "card work")
            if doc.get("host_branch_moved"):
                _move_host(origin)
            if doc.get("host_worktree"):
                wt = self.dir / f"host-wt-{n}"
                git(origin, "worktree", "add", "-q", str(wt), BRANCH)
                if doc["host_worktree"] == "dirty":
                    (wt / "README.md").write_text("uncommitted\n")
        return origin, ws

    def _check_publish(self, row, origin, ws, tid, task):
        expect = row["expect"]
        ok = epic_sync._publish(self.conn, self.cfg, task, ws, now=1)
        on_host = subprocess.run(
            ["git", "merge-base", "--is-ancestor", workspace.head(ws),
             f"refs/heads/{BRANCH}"], cwd=str(origin)).returncode == 0
        self.assertEqual(ok, expect["card_marked_done"], row["id"])
        self.assertEqual(on_host, expect["commit_on_host_branch"], row["id"])
        if "block_kind" in expect:
            got = self.conn.execute(
                "SELECT block_kind FROM tasks WHERE id=?", (tid,)).fetchone()[0]
            self.assertEqual(got, expect["block_kind"], row["id"])

    def _run_idle(self, doc, ws):
        """Seed the board per the document, run `sync_idle`, return the clone HEAD from before."""
        for table in ("tasks", "task_events", "guard_state"):   # each row starts from a clean board
            self.conn.execute(f"DELETE FROM {table}")
        if doc.get("card_holds_clone"):
            tid = db.new_task(self.conn, title="live", assignee="dev", branch=BRANCH,
                              status="running")
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, metadata)"
                " VALUES (?, 'implementer', 'running', 1, ?)",
                (tid, json.dumps({"start_sha": workspace.head(ws)})))
        before = workspace.head(ws)
        cfg = {"epics": [BRANCH], "board": "b"}
        if doc.get("clean_fails"):
            with mock.patch.object(epic_clean, "_backup", side_effect=RuntimeError("no disk")):
                epic_sync.sync_idle(self.conn, cfg, ws.parent, now=1)
        else:
            epic_sync.sync_idle(self.conn, cfg, ws.parent, now=1)
        return before

    def _check_idle(self, row, origin, ws):
        """`epic_sync.sync_idle`, the tick's idle-epic sync, on the same git fixtures. The
        fixture's own `after_gate_pass` card row is left in `review` unless the document says a
        live card holds the clone (a running card with a pinned `start_sha`)."""
        doc, expect = row["given"]["document"], row["expect"]
        before = self._run_idle(doc, ws)
        main_tip = git(origin, "rev-parse", "main").strip()   # real main, not the clone's stale ref
        has_main = subprocess.run(
            ["git", "merge-base", "--is-ancestor", main_tip, "HEAD"], cwd=str(ws),
            capture_output=True).returncode == 0
        self.assertEqual(has_main, expect["clone_contains_origin_main"], row["id"])
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (f"epic:{BRANCH}",))]
        wanted = expect["event"]
        self.assertEqual(wanted in kinds if wanted else not kinds, True, row["id"])
        held = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE block_kind IS NOT NULL").fetchone()[0]
        # an idle sync never holds a card
        self.assertEqual(held > 0, expect["card_held"], row["id"])
        if expect.get("clone_left_clean"):
            status = workspace._git("status", "--porcelain", ws=ws).stdout.strip()
            self.assertEqual(status, "", row["id"])
            self.assertFalse((workspace.gitdir(ws) / "MERGE_HEAD").exists(), row["id"])
            self.assertEqual(workspace.head(ws), before, row["id"])
        if not expect["clone_contains_origin_main"]:
            self.assertEqual(workspace.head(ws), before, row["id"])

    def _check_lane_leftover(self, row, ws, tid):
        """`tick_runs._guard_launch` on a reused clone made dirty by the fixture; "own" seeds a
        run of the launching card, so the dirt is its own failed round."""
        doc, expect = row["given"]["document"], row["expect"]
        for old in (ws.parent.parent / "epic-backups").glob("*.patch"):
            old.unlink()    # the fixture's backup dir is shared by the rows
        if doc["leftover"] == "own":
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at)"
                              " VALUES (?, 'implementer', 'failed', 1)", (tid,))
        card = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        with mock.patch.object(epic_clean, "_backup", **(
                {"side_effect": RuntimeError("no disk")} if doc.get("backup_fails") else
                {"wraps": epic_clean._backup})):
            got = tick_runs._guard_launch(self.conn, {"board": "b"}, card, ws, 1)
        self.assertEqual(got is not None, expect["refused"], row["id"])
        clean = workspace._git("status", "--porcelain", ws=ws).stdout.strip() == ""
        self.assertEqual(clean, expect["clone_left_clean"], row["id"])
        backups = list((ws.parent.parent / "epic-backups").glob("*.patch"))
        self.assertEqual(bool(backups), expect["backup_exists"], row["id"])

    def test_every_example_matches_its_oracle(self):
        lines = (paths.INTENT / "epic_sync.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        checks = {json.loads(line)["given"]["document"]["check"] for line in lines}
        self.assertIn("epic_sync_idle", checks)   # the idle-sync rows must stay in the contract
        for line in lines:
            row = json.loads(line)
            doc, expect = row["given"]["document"], row["expect"]
            with self.subTest(row["id"]):
                origin, ws = self._fixture(doc)
                tid = db.new_task(self.conn, title="t", assignee="dev", branch=BRANCH, status="review")
                task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                if doc["check"] == "epic_publish":
                    self._check_publish(row, origin, ws, tid, task)
                    continue
                if doc["check"] == "lane_leftover":
                    self._check_lane_leftover(row, ws, tid)
                    continue
                if doc["check"] == "epic_sync_idle":
                    self._check_idle(row, origin, ws)
                    continue
                ok = epic_sync.after_gate_pass(self.conn, self.cfg, task, ws, now=1)
                self.assertEqual(ok, expect["card_marked_done"], row["id"])
                synced = self.conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id=? AND kind='epic_synced'", (tid,)).fetchone()
                self.assertEqual(synced is not None, expect.get("event") == "epic_synced", row["id"])
                if "block_kind" in expect:
                    got = self.conn.execute("SELECT block_kind FROM tasks WHERE id=?", (tid,)).fetchone()[0]
                    self.assertEqual(got, expect["block_kind"], row["id"])
                if expect.get("clone_left_clean"):
                    self.assertEqual(workspace._git("status", "--porcelain", ws=ws).stdout.strip(), "", row["id"])
                    self.assertFalse((workspace.gitdir(ws) / "MERGE_HEAD").exists(), row["id"])


class EpicSyncHook(TickCase):
    """t_e905062b: `epic_sync.after_gate_pass`/`finish_gate_pass`, called from `Tick._verdict`
    right after a card's gate-pass commit. Tick-path oracle: the hook fires exactly once per
    gate pass on the real clone `_launch`/`_verdict` use, not at all on a failed round, and a
    real conflict actually blocks the card end to end rather than only at the unit level."""

    def setUp(self):
        super().setUp()
        origin = self.home / "origin"
        tick_git(origin, "branch", "main", TICK_BRANCH)
        tick_git(origin, "checkout", "-q", "main")
        (origin / "elsewhere.txt").write_text("from main\n")
        tick_git(origin, "add", ".")
        tick_git(origin, "commit", "-qm", "main moved on")
        tick_git(origin, "checkout", "-q", TICK_BRANCH)

    def test_the_hook_fires_exactly_once_after_a_gate_pass(self):
        self.script["implementer"] = [[IMPL_DONE]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        rows = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='epic_synced'",
                                 (self.tid,)).fetchall()
        self.assertEqual(len(rows), 1)
        ws = paths.board_dir("test", self.home) / "work" / workspace._slug(TICK_BRANCH)
        self.assertIn("elsewhere.txt", workspace._git("ls-files", ws=ws).stdout)

    def test_the_cost_span_is_pinned_before_the_sync_so_main_lines_are_not_the_cards(self):
        """The cost span's tip must be pinned before `after_gate_pass` merges origin/main and moves
        HEAD; a lazily resolved "HEAD" would count main's lines as the card's."""
        self.script["implementer"] = [[IMPL_DONE]]      # commits nothing; the sync still moves HEAD
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        row = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='cost'",
                                (self.tid,)).fetchone()
        self.assertEqual(json.loads(row[0])["net"], [0, 0])

    def test_no_sync_event_on_a_failed_review_round(self):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["validator"] = [[VAL_FAIL_ONE]]
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='epic_synced'", (self.tid,)).fetchone())

    def test_a_conflict_blocks_the_card_with_no_completed_event_and_no_done_notice(self):
        """A conflict at the tick path blocks the card; no `completed` event, no done notice."""
        origin = self.home / "origin"
        tick_git(origin, "checkout", "-q", "main")
        (origin / "README.md").write_text("main changed it\n")
        tick_git(origin, "add", ".")
        tick_git(origin, "commit", "-qm", "main edits README")
        tick_git(origin, "checkout", "-q", TICK_BRANCH)
        self.script["implementer"] = [[IMPL_DONE]]
        self.hooks["implementer"] = lambda job, sp, run: (
            Path(job["workspace"]) / "README.md").write_text("the card changed it too\n")
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "blocked":
                break
        row = self.task()
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["block_kind"], epic_sync.CONFLICT_KIND)
        self.assertEqual(row["failure_count"], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='completed'", (self.tid,)).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM outbox WHERE thread_key=? AND body LIKE '%done%'", (self.tid,)).fetchone())
