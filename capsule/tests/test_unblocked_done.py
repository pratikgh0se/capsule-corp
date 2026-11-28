# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   DoneBranches.test_detect_no_longer_takes_legacy_home
"""Landed and superseded branches are cleanup, not work: listed, never counted.

Real throwaway git repo + real queue markers, read through `unblocked.detect` and then through
the two other consumers of the same detector (the stop gate and the keeper watchdog).
"""
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from capsule import keeper_watch, unblocked, unblocked_stop
from capsule.tests.test_unblocked import git, make_repo


def fixture(states):
    """A repo with one ahead `capsule/<name>` branch per `{name: marker state or None}`."""
    repo, home = make_repo(), Path(tempfile.mkdtemp())
    (home / "config.json").write_text(json.dumps(
        {"unblocked": {"boards": [], "repos": [str(repo)], "runs": []}}))
    for name, state in states.items():
        git(repo, "checkout", "-q", "-b", f"capsule/{name}", "main")
        (repo / f"{name}.txt").write_text(name)
        git(repo, "add", f"{name}.txt")
        git(repo, "commit", "-q", "-m", name)
        if state:
            marker = home / "queue" / repo.name / "capsule" / f"{name}.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"state": state}))
    git(repo, "checkout", "-q", "main")
    return home


def idle_sync_then_main_moves(home, name):
    """Main lands `<name>.txt` with other content; the branch merges that main; main moves on."""
    repo = Path(json.loads((home / "config.json").read_text())["unblocked"]["repos"][0])
    git(repo, "checkout", "-q", "main")
    (repo / f"{name}.txt").write_text("resolved")
    git(repo, "add", f"{name}.txt")
    git(repo, "commit", "-q", "-m", f"landed {name}")
    git(repo, "checkout", "-q", f"capsule/{name}")
    git(repo, "merge", "-q", "--no-ff", "-X", "theirs", "-m", "idle sync", "main")
    git(repo, "checkout", "-q", "main")
    (repo / "later.txt").write_text("later")
    git(repo, "add", "later.txt")
    git(repo, "commit", "-q", "-m", "main moves on")


def detect(home):
    return unblocked.detect(capsule_home=home)


class DoneBranches(unittest.TestCase):
    def test_landed_branch_is_not_counted(self):
        items = detect(fixture({"a": "landed"}))
        self.assertEqual(unblocked.summarize(items), (0, 0))
        self.assertEqual(len(items), 1)    # still listed

    def test_superseded_branch_is_not_counted(self):
        items = detect(fixture({"a": "superseded"}))
        self.assertEqual(unblocked.summarize(items), (0, 0))
        self.assertEqual(len(items), 1)

    def test_built_unreviewed_branch_is_still_counted(self):
        items = detect(fixture({"a": "built-unreviewed", "b": None, "c": "landed"}))
        self.assertEqual(unblocked.summarize(items), (2, 2))

    def test_done_section_lists_excluded_branches_outside_the_table(self):
        out = unblocked.render_table(detect(fixture({"a": "landed", "b": "superseded", "c": None})))
        head, _, tail = out.partition("-- done branches (cleanup) --")
        self.assertIn("UNBLOCKED 1 UNOWNED 1", head)
        self.assertIn("(landed)", tail)
        self.assertIn("(superseded)", tail)
        self.assertNotIn("(landed)", head)
        self.assertNotIn("(superseded)", head)
        self.assertIn("(built-unreviewed)", head)

    def test_commits_after_the_marker_bring_the_branch_back_as_work(self):
        for state in ("landed", "superseded"):
            home = fixture({"a": state})
            os.utime(next(home.glob("queue/*/capsule/a.json")), (1000, 1000))    # older than tip
            items = detect(home)
            self.assertEqual(unblocked.summarize(items), (1, 1))
            self.assertIn(f"({state}, new commits)", items[0].item)
            doc = json.loads(unblocked.render_json(items))
            state = Path(tempfile.mkdtemp()) / "s.json"
            block, _ = unblocked_stop.should_block(doc, state_path=state)
            self.assertTrue(block)

    def test_cherry_picked_epic_is_not_flagged_but_ahead_epic_is(self):
        home = fixture({"picked": "landed", "ahead": "landed"})
        repo = Path(json.loads((home / "config.json").read_text())["unblocked"]["repos"][0])
        git(repo, "checkout", "-q", "main")
        (repo / "other.txt").write_text("other")
        git(repo, "add", "other.txt")
        git(repo, "commit", "-q", "-m", "other")    # main moves on: the pick gets a new sha
        git(repo, "cherry-pick", "capsule/picked")
        git(repo, "checkout", "-q", "capsule/ahead")
        (repo / "extra.txt").write_text("extra")
        git(repo, "add", "extra.txt")
        git(repo, "commit", "-q", "-m", "extra")
        git(repo, "checkout", "-q", "main")
        for name in ("picked", "ahead"):
            os.utime(next(home.glob(f"queue/*/capsule/{name}.json")), (1000, 1000))
        items = {i.item.split()[0].split(":")[1]: i for i in detect(home)}
        self.assertEqual(items["capsule/picked"].state, "done")
        self.assertEqual(items["capsule/ahead"].state, "unblocked")
        self.assertIn("new commits", items["capsule/ahead"].item)

    def test_squash_merged_epic_with_identical_tree_is_not_flagged(self):
        home = fixture({"sq": "landed"})
        repo = Path(json.loads((home / "config.json").read_text())["unblocked"]["repos"][0])
        git(repo, "merge", "--squash", "capsule/sq")
        git(repo, "commit", "-q", "-m", "squash sq")
        os.utime(next(home.glob("queue/*/capsule/sq.json")), (1000, 1000))
        items = detect(home)
        self.assertEqual(unblocked.summarize(items), (0, 0))
        self.assertEqual(items[0].state, "done")

    def test_idle_sync_merge_of_older_main_is_not_flagged_but_new_commit_is(self):
        home = fixture({"sync": "landed", "real": "landed"})
        repo = Path(json.loads((home / "config.json").read_text())["unblocked"]["repos"][0])
        idle_sync_then_main_moves(home, "sync")
        git(repo, "checkout", "-q", "capsule/real")
        (repo / "extra.txt").write_text("extra")    # `real` has a genuine new commit
        git(repo, "add", "extra.txt")
        git(repo, "commit", "-q", "-m", "extra")
        git(repo, "checkout", "-q", "main")
        for name in ("sync", "real"):
            os.utime(next(home.glob(f"queue/*/capsule/{name}.json")), (1000, 1000))
        items = {i.item.split()[0].split(":")[1]: i for i in detect(home)}
        self.assertEqual(items["capsule/sync"].state, "done")
        self.assertEqual(items["capsule/real"].state, "unblocked")

    def test_new_commits_are_judged_by_committer_time_not_author_time(self):
        marker_time, early, late = 1_500_000_000, 1_000_000_000, 2_000_000_000
        for author, committer, counts in ((early, late, True), (late, early, False)):
            home = fixture({"a": "landed"})
            repo = Path(json.loads((home / "config.json").read_text())["unblocked"]["repos"][0])
            git(repo, "checkout", "-q", "capsule/a")
            (repo / "more.txt").write_text("more")
            git(repo, "add", "more.txt")
            dates = {"GIT_AUTHOR_DATE": f"{author} +0000",
                     "GIT_COMMITTER_DATE": f"{committer} +0000"}
            with unittest.mock.patch.dict(os.environ, dates):
                git(repo, "commit", "-q", "-m", "more")
            git(repo, "checkout", "-q", "main")
            os.utime(next(home.glob("queue/*/capsule/a.json")), (marker_time, marker_time))
            self.assertEqual(unblocked.summarize(detect(home)), (1, 1) if counts else (0, 0))

    def test_no_done_branches_means_no_done_heading(self):
        self.assertNotIn("done branches", unblocked.render_table(detect(fixture({"c": None}))))

    def test_stop_gate_gets_the_reduced_count(self):
        home = fixture({"a": "landed", "b": "superseded"})
        doc = json.loads(unblocked.render_json(detect(home)))
        state = Path(tempfile.mkdtemp()) / "stop.json"
        self.assertEqual(unblocked_stop.should_block(doc, state_path=state), (False, []))
        home = fixture({"a": "landed", "c": None})
        doc = json.loads(unblocked.render_json(detect(home)))
        block, pairs = unblocked_stop.should_block(doc, state_path=state)
        self.assertTrue(block)
        self.assertEqual(len(pairs), 1)

    def test_keeper_watchdog_gets_the_reduced_count(self):
        def count(home):
            return keeper_watch.waiting(home, detect=lambda **kw: unblocked.detect(
                **kw))
        self.assertEqual(count(fixture({"a": "landed", "b": "superseded"})), 0)
        self.assertEqual(count(fixture({"a": "landed", "c": None})), 1)

    def test_detect_no_longer_takes_legacy_home(self):
        """D244: the old board fallback is gone, so the kwarg is too."""
        with self.assertRaises(TypeError):
            unblocked.detect(capsule_home=Path(tempfile.mkdtemp()), legacy_home=Path("/no-home"))


if __name__ == "__main__":
    unittest.main()
