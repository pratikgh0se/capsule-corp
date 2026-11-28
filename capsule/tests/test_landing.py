"""C07b (t_0fc44cdb): the landing step behind `lander.mode`. Real git over a temp bare remote (never
the real origin), a real release checkout cloned from it, real shell suites; nothing is mocked.
Oracle 1 is the outcome dict, the board rows and the marker; oracle 2 reads the bare remote's
`main`, the release checkout and the dev checkout's refs with plain git."""
import contextlib
import io
import json
import subprocess
import time
import unittest

from capsule import cost, db, intent, landing, paths, seats
from capsule.tests.test_lander import BRANCH, World, git

NOW = int(time.time())


class Landing(World):
    RELEASE = True

    def setUp(self):
        super().setUp()
        self.release = self.dir / "release"
        self.home = self.dir / "home"
        self.cfg.update({"board": "b", "caps": {}})
        self.cfg["lander"]["mode"] = "land"
        board = self.dir / "board.db"
        db.create_board(board)
        self.conn = db.connect(board)
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="t", assignee="dev", branch=BRANCH, status="review")

    def land(self, fetch=False):
        """One pass through the CLI: lander.run, then the landing. `fetch=False` (the default) is
        `--no-fetch`: the dev repo's `origin/main` stays as it was, so a rival push made after
        the check is not seen until the next pass."""
        cfg = self.dir / "cfg.json"
        cfg.write_text(json.dumps(self.cfg))
        argv = ["land", "--db", str(self.dir / "board.db"), "--task", self.tid, "--repo",
                str(self.repo), "--release", str(self.release), "--config", str(cfg), "--home",
                str(self.home)] + ([] if fetch else ["--no-fetch"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            landing.main(argv)
        return json.loads(out.getvalue())

    def remote_main(self):
        return git(self.remote, "rev-parse", "main")

    def rival(self, name="rival.txt", text="rival\n"):
        other = self.dir / "other"
        if not other.exists():
            subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True,
                           capture_output=True)
        (other / name).write_text(text)
        git(other, "add", "-A")
        git(other, "commit", "-qm", "rival")
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def kinds(self):
        return [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]

    def marker(self):
        return json.loads((self.home / "queue" / "b" / f"{BRANCH}.json").read_text())


class Green(Landing):
    def test_a_checked_branch_is_pushed_from_the_release_which_itself_does_not_move(self):
        head = self.put("a.txt", "work\n", "card work")
        main_before = git(self.repo, "rev-parse", "main")
        release_before = git(self.release, "rev-parse", "HEAD")
        got = self.land()
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        self.assertEqual(self.remote_main(), head)
        self.assertEqual(git(self.release, "rev-parse", "HEAD"), release_before)   # restart's job
        self.assertEqual(git(self.release, "status", "--porcelain"), "")
        self.assertEqual(git(self.repo, "rev-parse", "main"), main_before)   # the dev checkout
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_the_marker_json_and_the_completed_event_carry_the_landing(self):
        head = self.put("a.txt", "work\n", "card work")
        self.land()
        marker = self.marker()
        self.assertEqual((marker["state"], marker["landed_sha"]), ("landed", head))
        self.assertIn("suites", marker)
        self.assertIn("deploy-ok", marker["deploy"])
        row = self.conn.execute("SELECT payload FROM task_events WHERE kind='completed'").fetchone()
        payload = json.loads(row[0])
        self.assertEqual(payload["commit"], head)
        self.assertTrue(payload["gates"])
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                           (self.tid,)).fetchone()[0], "done")

    def test_cost_counts_the_landed_card_because_the_event_has_gates(self):
        self.put("a.txt", "work\n", "card work")
        self.land()
        other = db.new_task(self.conn, title="o", assignee="dev", status="done")
        got = [r[0] for r in self.conn.execute(cost._LANDED, (other, 20))]
        self.assertEqual(got, [self.tid])

    def test_dry_run_is_the_default_and_moves_nothing(self):
        self.put("a.txt", "work\n", "card work")
        before = self.remote_main()
        del self.cfg["lander"]["mode"]
        self.assertEqual(self.land()["outcome"], "dry-run")
        self.assertEqual(self.remote_main(), before)
        self.assertEqual(git(self.release, "rev-parse", "HEAD"), before)

    def test_a_held_verdict_never_lands(self):
        self.put("docs/ZENKAI.md", "x\n", "protected")
        before = self.remote_main()
        got = self.land()
        self.assertEqual(got["outcome"], "not_checked")
        self.assertEqual(self.remote_main(), before)


class Refused(Landing):
    def test_a_rejected_push_changes_nothing_and_is_never_forced(self):
        self.put("a.txt", "work\n", "card work")
        rival = self.rival()
        got = self.land()
        self.assertEqual(got["outcome"], "push_rejected")
        self.assertEqual(self.remote_main(), rival)
        self.assertNotIn("completed", self.kinds())
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                           (self.tid,)).fetchone()[0], "review")
        self.assertFalse(db.guard_get(self.conn, landing.BUSY)[0]["busy"])

    def test_the_next_pass_rechecks_and_sends_the_stale_branch_back(self):
        self.put("a.txt", "work\n", "card work")
        self.rival("a.txt")
        self.assertEqual(self.land()["outcome"], "push_rejected")
        self.assertEqual(self.land(fetch=True)["outcome"], "not_checked")   # behind, and conflicts
        self.assertEqual(self.check()["verdict"], "wait")   # needs a rebase

    def test_a_stale_dirty_release_checkout_does_not_block_a_landing_and_is_left_alone(self):
        head = self.put("a.txt", "work\n", "card work")
        (self.release / "README.md").write_text("dirty\n")
        before = git(self.release, "rev-parse", "HEAD")
        self.assertEqual(self.land()["outcome"], "landed")
        self.assertEqual(self.remote_main(), head)
        self.assertEqual(git(self.release, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.release, "status", "--porcelain"), "M README.md")


class Exclusion(Landing):
    def test_a_fresh_busy_flag_means_no_second_landing(self):
        self.put("a.txt", "work\n", "card work")
        db.guard_set(self.conn, landing.BUSY, {"busy": True, "task": "t_other", "at": NOW - 10})
        before = self.remote_main()
        self.assertEqual(self.land()["outcome"], "busy")
        self.assertEqual(self.remote_main(), before)

    def test_a_stale_busy_flag_is_taken_over(self):
        self.put("a.txt", "work\n", "card work")
        db.guard_set(self.conn, landing.BUSY,
                     {"busy": True, "task": "t_other", "at": NOW - landing.BUSY_TTL - 1})
        self.assertEqual(self.land()["outcome"], "landed")

    def test_only_one_of_two_racing_acquires_wins(self):
        _, version = db.guard_get(self.conn, landing.BUSY)
        first = db.guard_set(self.conn, landing.BUSY, {"busy": True, "at": NOW},
                             expect_version=version)
        second = db.guard_set(self.conn, landing.BUSY, {"busy": True, "at": NOW},
                              expect_version=version)
        self.assertEqual((first, second), (True, False))

    def test_the_lander_takes_one_heavy_seat(self):
        self.assertEqual(seats.counts(self.conn, self.home, "b", NOW)["heavy"], 0)
        landing.acquire(self.conn, self.tid, NOW)
        self.assertEqual(seats.counts(self.conn, self.home, "b", NOW)["heavy"], 1)

    def test_a_crashed_landers_flag_frees_its_seat_after_the_ttl(self):
        flag = {"busy": True, "task": "t_dead", "at": NOW - seats.LANDER_TTL - 1}
        db.guard_set(self.conn, landing.BUSY, flag)
        self.assertEqual(seats.counts(self.conn, self.home, "b", NOW)["heavy"], 0)
        db.guard_set(self.conn, landing.BUSY, {**flag, "at": NOW - 5})
        self.assertEqual(seats.counts(self.conn, self.home, "b", NOW)["heavy"], 1)

    def test_another_boards_fresh_flag_means_busy(self):
        self.put("a.txt", "work\n", "card work")
        other = paths.board_db("other", self.home)
        other.parent.mkdir(parents=True)
        db.create_board(other)
        conn = db.connect(other)
        db.guard_set(conn, landing.BUSY, {"busy": True, "task": "t_o", "at": NOW - 5})
        conn.close()
        before = self.remote_main()
        self.assertEqual(self.land()["outcome"], "busy")
        self.assertEqual(self.remote_main(), before)

    def test_no_free_heavy_seat_means_no_landing(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["caps"] = {"max_heavy": 1}
        other = db.new_task(self.conn, title="run", assignee="dev", status="ready")
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
        before = self.remote_main()
        self.assertEqual(self.land()["outcome"], "no_seat")
        self.assertEqual(self.remote_main(), before)


class Drift(Landing):
    def test_deploy_drift_after_the_push_files_a_needs_input_line_and_gates_the_next_landing(self):
        self.cfg["lander"]["deploy_check"] = "echo DRIFT x; exit 1"
        self.put("a.txt", "work\n", "card work")
        head = self.put("b.txt", "more\n", "more work")
        got = self.land()
        self.assertEqual((got["outcome"], got["deploy"]), ("landed", "drift"))
        self.assertEqual(self.remote_main(), head)
        lines = [r[0] for r in self.conn.execute("SELECT body FROM outbox")]
        self.assertTrue(any("DRIFT x" in line for line in lines), lines)
        self.assertIn("DRIFT", self.marker()["deploy"])
        self.assertEqual(self.land()["outcome"], "held_deploy_drift")

    def test_the_hold_clears_itself_once_deploy_check_is_clean(self):
        self.cfg["lander"]["deploy_check"] = "echo DRIFT x; exit 1"
        self.put("a.txt", "work\n", "card work")
        self.land()
        self.assertTrue(db.guard_get(self.conn, landing.HOLD)[0])
        head = self.put("c.txt", "next\n", "next work")
        self.cfg["lander"]["deploy_check"] = "echo clean"
        self.assertEqual(self.land()["outcome"], "landed")
        self.assertEqual(self.remote_main(), head)
        self.assertIsNone(db.guard_get(self.conn, landing.HOLD)[0])


class Replay(Landing):
    def test_a_card_behind_main_is_replayed_pushed_and_recorded_under_the_new_sha(self):
        card = self.put("a.txt", "work\n", "card work")
        rival = self.rival()
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        landed = self.remote_main()
        self.assertNotIn(landed, (card, rival))
        self.assertEqual(git(self.remote, "rev-parse", "main^"), rival)    # ff-only on top
        self.assertEqual(git(self.remote, "show", "main:a.txt"), "work")
        self.assertEqual(self.marker()["landed_sha"], landed)
        done = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='completed'").fetchone()[0])
        self.assertEqual(done["commit"], landed)

    def test_a_card_already_on_main_is_completed_as_already_on_main_with_no_push(self):
        self.put("a.txt", "work\n", "card work")
        main = self.rival("a.txt", "work\n")        # a human landed the same change
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("already_on_main", False))
        self.assertEqual(self.remote_main(), main)
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE id=?",
                                           (self.tid,)).fetchone()[0], "done")
        self.assertEqual(self.marker()["commits"], [])

    def test_a_replayed_card_is_not_rechecked_in_dry_run_every_pass(self):
        sha = self.put("a.txt", "work\n", "card work")
        self.rival()
        self.cfg["lander"]["mode"] = "dry-run"
        self.assertEqual(self.land(fetch=True)["outcome"], "dry-run")
        row = self.conn.execute("SELECT payload FROM task_events WHERE kind='landing_checked'")
        self.assertEqual(json.loads(row.fetchone()[0])["card_commit"], sha)
        self.assertTrue(landing._seen(self.conn, "dry-run", self.tid, sha, self.repo, BRANCH))


class FullReplay(Landing):
    """A reworked card (original plus fix-up, both named with its id) lands whole."""

    def card(self):
        first = self.put("a.txt", "work\n", f"card work ({self.tid}, capsule run 1)")
        return first, self.put("a.txt", "work\nfix\n", f"card fix-up ({self.tid}, capsule run 2)")

    def test_both_commits_land_when_main_moved_and_the_marker_records_both(self):
        self.card()
        rival = self.rival()
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        self.assertEqual(git(self.remote, "show", "main:a.txt"), "work\nfix")
        self.assertEqual(git(self.remote, "rev-list", "--count", f"{rival}..main"), "2")
        self.assertEqual(len(self.marker()["commits"]), 2)

    def test_both_commits_land_when_main_did_not_move(self):
        first, second = self.card()
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        self.assertEqual(self.remote_main(), second)
        self.assertEqual(self.marker()["commits"], [first, second])

    def test_a_landed_two_commit_card_that_goes_red_is_reverted_whole(self):
        self.card()
        self.rival()
        self.assertEqual(self.land(fetch=True)["outcome"], "landed")
        self.cfg["contract_commands"] = ["exit 1"]
        out = landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release, now=NOW + 5)
        self.assertEqual(out["outcome"], "reverted")
        self.assertEqual(git(self.remote, "ls-tree", "-r", "--name-only", "main").split(),
                         ["README.md", "rival.txt"])


class ReworkAfterLanding(Landing):
    """A card landed by replay, then reworked: the next replay starts after what that landing
    carried, unless the landing was undone (then the original is wanted again)."""

    def msg(self, what, n):
        return f"{what} ({self.tid}, capsule run {n})"

    def landed(self):
        """The original on the epic, main moved, the card landed by replay: its landed sha."""
        self.first = self.put("a.txt", "one\ntwo\n", self.msg("card work", 1))
        self.rival()
        self.assertEqual(self.land(fetch=True)["outcome"], "landed")
        self.assertEqual(db.guard_get(self.conn, landing.LANDED + self.tid)[0]["card"], self.first)
        return self.remote_main()

    def rework(self):
        """The fix-up on top of the original; what main gained by landing it, newest last."""
        before = self.remote_main()
        self.put("b.txt", "fix\n", self.msg("card fix-up", 2))
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        return git(self.remote, "log", "--reverse", "--format=%s", f"{before}..main").splitlines()

    def test_an_original_already_landed_by_replay_is_not_picked_again_after_main_changed_it(self):
        self.landed()
        other = self.dir / "other"
        git(other, "pull", "-q", "--ff-only")
        (other / "a.txt").write_text("other\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "other card (t_other, capsule run 1)")
        git(other, "push", "-q", "origin", "main")
        self.assertEqual(self.rework(), [self.msg("card fix-up", 2)])
        self.assertEqual(git(self.remote, "show", "main:a.txt"), "other")
        self.assertEqual(git(self.remote, "show", "main:b.txt"), "fix")

    def test_a_red_revert_then_rework_replays_the_original_and_the_fix_up(self):
        self.landed()
        self.cfg["contract_commands"] = ["exit 1"]
        out = landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release, now=NOW + 5)
        self.assertEqual(out["outcome"], "reverted")
        self.cfg["contract_commands"] = ["echo suite-one-ok"]
        self.assertEqual(self.rework(), [self.msg("card work", 1), self.msg("card fix-up", 2)])
        self.assertEqual(git(self.remote, "show", "main:a.txt"), "one\ntwo")

    def test_a_revert_on_main_then_rework_replays_the_original_and_the_fix_up(self):
        landed = self.landed()
        other = self.dir / "other"
        git(other, "pull", "-q", "--ff-only")
        git(other, "revert", "--no-edit", landed)
        git(other, "push", "-q", "origin", "main")
        self.assertEqual(self.rework(), [self.msg("card work", 1), self.msg("card fix-up", 2)])
        self.assertEqual(git(self.remote, "show", "main:a.txt"), "one\ntwo")


class IntentExamples(Landing):
    """Z1: the row-600 examples in contracts/intent/lander.jsonl replayed through `landing`."""

    def replay(self, case):
        sha = self.put("a.txt", "work\n", "card work")
        """(outcome, pushed): pushed is read off the bare remote, not taken from the outcome dict;
        for the red case it is whether a revert commit (tree back to README only) is on it."""
        if case.startswith("day_branch"):       # LAND-03: read the bare remote, main must not move
            main = self.remote_main()
            if case == "day_branch_land":
                git(self.remote, "branch", "capsule/daily", "main")
            self.cfg["lander"]["branch"] = "capsule/daily"
            got = self.land(fetch=True)
            return got["outcome"], (git(self.remote, "rev-parse", "capsule/daily") == sha
                                    and self.remote_main() == main)
        if case == "land_push_rejected":
            self.rival()
            got = self.land()
            return got["outcome"], self.remote_main() == sha
        got = self.land()
        if case == "land_green":
            return got["outcome"], self.remote_main() == sha
        self.cfg["contract_commands"] = ["exit 1"]
        out = landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release, now=NOW + 5)
        tree = git(self.remote, "ls-tree", "-r", "--name-only", "main")
        subprocess.run(["git", "merge-base", "--is-ancestor", sha, self.remote_main()],
                       cwd=str(self.remote), check=True)    # history kept: no reset, no force
        return out["outcome"], self.remote_main() != sha and tree == "README.md"

    def test_the_landing_examples(self):
        shipped = [e for e in intent.load("lander") if "outcome" in e["expect"]]
        self.assertEqual(len(shipped), 5)
        for ex in shipped:
            self.setUp()
            got = self.replay(ex["given"]["document"]["case"])
            self.assertEqual(got, (ex["expect"]["outcome"], ex["expect"]["pushed"]), ex["id"])


class ReplayExamples(Landing):
    """Z1: the W205 replay rows in contracts/intent/lander.jsonl."""

    def replay(self, case):
        self.put("a.txt", "work\n", "card work")
        self.rival("a.txt" if case == "replay_conflict" else "rival.txt")
        before = self.remote_main()
        landed = self.land(fetch=True)["outcome"] == "landed"
        return landed, self.remote_main() != before, self.detail(self.check(), "sync")

    def test_the_replay_examples(self):
        shipped = [e for e in intent.load("lander") if "replay" in e["expect"]]
        self.assertEqual(len(shipped), 2)
        for ex in shipped:
            self.setUp()
            landed, pushed, detail = self.replay(ex["given"]["document"]["case"])
            want = ex["expect"]
            self.assertEqual((landed, pushed), (want["replay"] == "landed", want["pushed"]),
                             ex["id"])
            if "files" in want:
                self.assertIn(want["files"], detail, ex["id"])


if __name__ == "__main__":
    unittest.main()
