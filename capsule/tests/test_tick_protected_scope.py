"""A hand merge of origin/main into a lane clone must not turn main's protected files into a
row-395 failure or a staged revert (inc_b8a1d115, inc_f6317f0b). `Tick._verdict` pins protected
paths to the card's first start_sha; main's newer copies, merged in after that sha, are the host's
change and stay. A protected edit the run itself makes still fails the round."""
import json
import subprocess
import tempfile
from pathlib import Path

from capsule import paths, workspace
from capsule.tests.test_tick import BRANCH, IMPL_DONE, TickCase, git

MAIN_COPY = "main's newer copy\n"


class HostMergeScope(TickCase):
    def setUp(self):
        super().setUp()
        self.origin = self.home / "origin"
        git(self.origin, "branch", "main")
        self.main_dir = Path(tempfile.mkdtemp(dir=self.home))
        subprocess.run(["git", "clone", "-q", "--branch", "main", str(self.origin),
                        str(self.main_dir)], check=True, capture_output=True)

    def main_changes_protected_file(self):
        (self.main_dir / "contracts").mkdir(exist_ok=True)
        (self.main_dir / "contracts" / "x.md").write_text(MAIN_COPY)
        git(self.main_dir, "add", ".")
        git(self.main_dir, "commit", "-qm", "main moves a protected file")
        git(self.main_dir, "push", "-q", "origin", "main")

    def review_after_hand_merge(self, plant=None, how="--no-ff", merge=None):
        """Run an implementer round whose clone gets origin/main merged in by hand (after the
        first start_sha is pinned), optionally plants `plant(ws)` too; returns (ws, comments)."""
        self.script["implementer"] = [[IMPL_DONE]]
        ws = paths.board_dir("test", self.home) / "work" / workspace._slug(BRANCH)

        def hand_merge(job, sp, run):
            w = Path(job["workspace"])
            self.main_changes_protected_file()
            if merge:
                merge(w)
            else:
                git(w, "fetch", "-q", "origin")
                git(w, "merge", "-q", how, "--no-edit", "origin/main")
            if plant:
                plant(w)
        self.hooks["implementer"] = hand_merge
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        return ws, [r[0] for r in self.conn.execute("SELECT body FROM task_comments")]

    def staged(self, ws):
        return subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=ws, check=True,
                              capture_output=True, text=True).stdout.split()

    def test_a_hand_merge_after_start_sha_leaves_mains_protected_files_unstaged(self):
        ws, comments = self.review_after_hand_merge()
        self.assertFalse(any("protected path changed" in c for c in comments), comments)
        self.assertEqual(self.staged(ws), [])
        self.assertEqual((ws / "contracts" / "x.md").read_text(), MAIN_COPY)

    def test_a_protected_edit_planted_after_the_hand_merge_still_fails_the_round(self):
        def plant(w):
            (w / "contracts" / "x.md").write_text("changed by a worker\n")
            (w / "contracts" / "y.md").write_text("planted by a worker\n")
        ws, comments = self.review_after_hand_merge(plant)
        for name in ("contracts/x.md", "contracts/y.md"):
            want = f"protected path changed (row 395): {name}"
            self.assertTrue(any(want in c for c in comments), comments)
        self.assertEqual((ws / "contracts" / "x.md").read_text(), MAIN_COPY)   # main's, not base's
        self.assertFalse((ws / "contracts" / "y.md").exists())
        self.assertEqual(self.staged(ws), [])

    def test_each_refused_path_gets_one_protected_path_refused_event_with_three_keys(self):
        def plant(w):
            (w / "contracts" / "y.md").write_text("planted by a worker\n")
        self.review_after_hand_merge(plant)
        rows = self.conn.execute("SELECT task_id, payload FROM task_events"
                                 " WHERE kind = 'protected_path_refused'").fetchall()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0][1])
        self.assertEqual(payload, {"path": "contracts/y.md", "task_id": rows[0][0],
                                   "stage": "review"})

    def test_a_clean_round_writes_no_protected_path_refused_event(self):
        self.review_after_hand_merge()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM task_events WHERE"
                                           " kind = 'protected_path_refused'").fetchone()[0], 0)

    def test_a_worker_putting_the_start_copy_back_over_mains_newer_file_fails_the_round(self):
        ws, comments = self.review_after_hand_merge(lambda w: (w / "contracts" / "x.md").unlink())
        want = "protected path changed (row 395): contracts/x.md"
        self.assertTrue(any(want in c for c in comments), comments)
        self.assertEqual((ws / "contracts" / "x.md").read_text(), MAIN_COPY)
        self.assertEqual(self.staged(ws), [])

    def test_a_fast_forward_onto_main_after_start_sha_is_not_a_protected_change(self):
        ws, comments = self.review_after_hand_merge(how="--ff-only")
        self.assertFalse(any("protected path changed" in c for c in comments), comments)
        self.assertEqual(self.staged(ws), [])
        self.assertEqual((ws / "contracts" / "x.md").read_text(), MAIN_COPY)

    def test_a_hand_resolved_conflict_keeps_the_hosts_copy_when_mains_tip_is_a_merge(self):
        (self.origin / "contracts").mkdir()   # the epic's own stamp, in start_sha already
        (self.origin / "contracts" / "x.md").write_text("epic stamp\n")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-qm", "epic stamp")

        def resolve(w):
            main = self.main_dir
            git(main, "checkout", "-q", "-b", "side")
            (main / "contracts" / "s.md").write_text("side\n")
            git(main, "add", ".")
            git(main, "commit", "-qm", "side")
            git(main, "checkout", "-q", "main")
            git(main, "merge", "-q", "--no-ff", "--no-edit", "side")   # main's tip is a merge
            git(main, "push", "-q", "origin", "main")
            git(w, "fetch", "-q", "origin")
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "merge", "--no-ff", "--no-edit", "origin/main"], cwd=w,
                           capture_output=True)
            (w / "contracts" / "x.md").write_text("resolved by the host\n")
            git(w, "add", ".")
            git(w, "commit", "-qm", "host resolves the conflict")
        ws, comments = self.review_after_hand_merge(merge=resolve)
        self.assertFalse(any("protected path changed" in c for c in comments), comments)
        self.assertEqual(self.staged(ws), [])
        self.assertEqual((ws / "contracts" / "x.md").read_text(), "resolved by the host\n")
