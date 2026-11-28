"""The lander's replay of a card whose lane holds other history: a by-hand merge of main between
the card's commits (t_bf927053), and a closed card's commits below it (t_07c99204)."""
import subprocess

from capsule import db, lander
from capsule.tests.test_lander import BRANCH, GIT, World, git


RUN = "7ae6612ccb734feeb285e930bd9e78a9"      # a real run id: uuid4().hex


class ReworkReplay(World):
    TID = "t_card0001"

    def msg(self, what, n=RUN, tid=None):
        return f"{what} ({tid or self.TID}, capsule run {n})"

    def replayed(self, result):
        """Subjects of the commits the check ran on, oldest first, above the range base."""
        return git(self.repo, "log", "--reverse", "--format=%s",
                   f"{result['base']}..{result['commit']}").splitlines()

    def closed_card_below(self, closed):
        """The lane holds main; a card (OTHER) has a commit on it, then this card's: a board
        where OTHER is `closed` by a person after its completion, or still an ordinary done card."""
        board = self.dir / "board.db"
        db.create_board(board)
        conn = db.connect(board)
        self.addCleanup(conn.close)
        other = db.new_task(conn, title="o", assignee="dev", branch=BRANCH, status="done")
        db.event(conn, other, "completed", {"commit": "x"}, now=1)
        if closed:
            db.event(conn, other, "closed", None, now=2)
        self.put("o.txt", "abandoned\n", self.msg("other work", tid=other))
        self.put("a.txt", "work\n", self.msg("card work"))
        return conn

    def test_a_card_closed_as_abandoned_below_this_one_is_not_carried_to_main(self):
        conn = self.closed_card_below(True)
        result = self.check(task_id=self.TID, conn=conn)
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.replayed(result), [self.msg("card work")])
        self.assertNotEqual(result["commit"], git(self.repo, "rev-parse", "HEAD"))
        self.assertNotIn("o.txt", git(self.repo, "ls-tree", "--name-only", result["commit"]))

    def test_an_ordinary_card_below_this_one_still_lands_with_it_unreplayed(self):
        conn = self.closed_card_below(False)
        result = self.check(task_id=self.TID, conn=conn)
        self.assertEqual(result["commit"], git(self.repo, "rev-parse", "HEAD"))

    def by_hand_merge(self, resolved, fix_file="b.txt", name="a.txt", fix_text="fix\n"):
        """The card's original on the lane, main then edits a.txt, the lane takes main by hand
        (a.txt resolved to `resolved`), and the card's fix-up is the tip."""
        self.put(name, "work\n", self.msg("card work"))
        git(self.repo, "checkout", "-q", "main")
        self.put(name, "main\n", "main edits a")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        subprocess.run([*GIT, "merge", "-q", "--no-commit", "main"], cwd=str(self.repo))
        (self.repo / name).write_text(resolved)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "by-hand merge of main")
        self.put(fix_file, fix_text, self.msg("card fix-up", 2))

    def test_a_by_hand_main_merge_between_the_commits_does_not_block_the_replay(self):
        self.by_hand_merge("main\n")
        self.behind("c.txt")
        result = self.check(task_id=self.TID)
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.replayed(result), [self.msg("card fix-up", 2)])
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:a.txt"), "main")
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:b.txt"), "fix")

    def test_a_replay_that_conflicts_for_a_real_reason_still_waits_after_a_by_hand_merge(self):
        self.by_hand_merge("main\n", fix_file="a.txt")
        self.behind("a.txt")
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_rebase"))
        self.assertIn("conflicts in a.txt", self.detail(result, "sync"))

    def test_a_by_hand_merge_resolves_for_a_path_with_a_space(self):
        self.by_hand_merge("main\n", name="my file.txt")
        self.behind("c.txt")
        result = self.check(task_id=self.TID)
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:my file.txt"), "main")

    def test_a_by_hand_merge_resolves_when_the_tip_deletes_the_file(self):
        self.by_hand_merge("main\n")
        git(self.repo, "rm", "-q", "a.txt", "b.txt")
        git(self.repo, "commit", "-qm", self.msg("card deletes a", 3))
        self.behind("c.txt")
        result = self.check(task_id=self.TID)
        self.assertEqual(result["verdict"], "checked")
        left = git(self.repo, "ls-tree", "--name-only", result["commit"])
        self.assertTrue("a.txt" not in left and "b.txt" not in left)
        self.assertIn("replayed alone", self.detail(result, "sync"))

    def test_a_by_hand_resolution_that_differs_from_main_lands_as_resolved(self):
        self.by_hand_merge("by hand\n")
        self.behind("c.txt")
        result = self.check(task_id=self.TID)
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:a.txt"), "by hand")
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:b.txt"), "fix")

    def test_a_file_main_changed_again_after_the_by_hand_merge_still_waits(self):
        self.by_hand_merge("by hand\n")
        self.behind("a.txt")
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_rebase"))

    def test_a_closed_card_below_is_found_whatever_the_run_id_looks_like(self):
        for run in ("r1", "7ae6612ccb734feeb285e930bd9e78a9"):
            with self.subTest(run=run):
                board = self.dir / f"board-{run}.db"
                db.create_board(board)
                conn = db.connect(board)
                self.addCleanup(conn.close)
                other = db.new_task(conn, title="o", assignee="dev", branch=BRANCH, status="done")
                db.event(conn, other, "closed", None, now=2)
                self.put(f"o{run}.txt", "x\n", self.msg("other work", run, other))
                head = git(self.repo, "rev-parse", "HEAD")
                base = git(self.repo, "rev-parse", "HEAD~1")
                self.assertTrue(lander._carries_closed(self.repo, conn, self.TID, head, base))
