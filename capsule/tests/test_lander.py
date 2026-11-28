# mutation-check: targets-predate-range 4d253485 -- a test-only mock fix; the long-title
#   behaviour already held on base ::
#   FullReplay.test_a_very_long_title_keeps_the_whole_suffix_and_the_lander_still_matches_it
"""C07a (t_2216591d): the landing checks as a dry-run step. Real git over a bare fixture remote,
real shell commands for the suites and gates; nothing is mocked. Oracle 1 is the verdict and its
per-check rows; oracle 2 reads the repo's refs and the remote's `main` with plain git, before and
after, to show the dry run never moved anything."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from capsule import db, epic_sync, intent, lander, workspace
from capsule.tests import tmpclean, world_seed
from capsule.tests.world_seed import BRANCH, GIT


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout.strip()


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


class World(unittest.TestCase):
    RELEASE = False     # a release clone too (the landing tests)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.remote = self.dir / "remote.git"
        self.repo = self.dir / "repo"
        world_seed.copy_into(self.dir, self.RELEASE)    # a private copy of one built world
        self.cfg = {"contract_commands": ["echo suite-one-ok"],
                    "lander": {"gates": ["echo gate-ok"], "deploy_check": "echo deploy-ok"}}

    def put(self, name, text, msg):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def refs(self):
        return (git(self.repo, "rev-parse", "main"), git(self.repo, "rev-parse", BRANCH),
                git(self.remote, "rev-parse", "main"))

    def behind(self, name="b.txt"):
        git(self.repo, "checkout", "-q", "main")
        self.put(name, "main moved\n", "main moved")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)

    def check(self, **kw):
        return lander.run(self.repo, BRANCH, cfg=kw.pop("cfg", self.cfg), **kw)

    def status(self, result):
        return {c["name"]: c["status"] for c in result["checks"]}

    def detail(self, result, name):
        return next(c["detail"] for c in result["checks"] if c["name"] == name)


class Green(World):
    def test_all_checks_pass_and_nothing_moves(self):
        self.put("a.txt", "work\n", "card work")
        before = self.refs()
        result = self.check()
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(set(self.status(result).values()), {"pass"})
        self.assertEqual(list(self.status(result)),
                         ["fetch", "sync", "protected", "stale_marker", "gates", "suites",
                          "deploy"])
        self.assertEqual(self.refs(), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        tails = " ".join(c["detail"] for c in result["checks"])
        self.assertIn("suite-one-ok", tails)
        self.assertIn("deploy-ok", tails)

    def test_the_throwaway_clone_is_gone_afterwards(self):
        self.put("a.txt", "work\n", "card work")
        marker = self.dir / "cwd-seen"
        self.cfg["contract_commands"] = [f"pwd > {marker}"]
        self.check()
        self.assertFalse(Path(marker.read_text().strip()).exists())
        self.assertNotEqual(Path(marker.read_text().strip()).resolve(), self.repo.resolve())


class Sync(World):
    def test_a_branch_behind_origin_main_with_a_conflict_waits_before_any_suite_runs(self):
        card = self.put("a.txt", "work\n", "card work")
        self.behind("a.txt")
        before = self.refs()
        result = self.check()
        self.assertEqual(result["verdict"], "wait")
        self.assertEqual(result["hold"], "lander_wait_rebase")
        self.assertEqual(self.status(result)["sync"], "wait")
        self.assertEqual(self.status(result)["suites"], "skipped")
        self.assertIn("conflicts in a.txt", self.detail(result, "sync"))   # names the files
        self.assertEqual((result["commit"], self.refs()), (card, before))

    def test_only_the_cards_own_commit_is_replayed_not_the_epic_history_under_it(self):
        """The epic holds another card, a merge of main, then this card: the replay carries the
        card's change alone (a range replay would drag in `o.txt` and trip on the merge)."""
        self.put("o.txt", "other card\n", "other card")
        git(self.repo, "checkout", "-q", "main")
        self.put("m.txt", "main\n", "main first")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge main", "main")
        card = self.put("a.txt", "work\n", "card work")
        self.behind()
        result = self.check()
        new = result["commit"]
        self.assertEqual((result["verdict"], result["card_commit"]), ("checked", card))
        self.assertNotEqual(new, card)
        self.assertEqual(git(self.repo, "diff", "--name-only", f"{new}^", new), "a.txt")
        self.assertEqual(git(self.repo, "rev-parse", f"{new}^"), result["base"])

    def test_a_card_commit_that_is_a_merge_waits_with_the_reason(self):
        git(self.repo, "checkout", "-q", "-b", "side", "main")
        self.put("s.txt", "side\n", "side")
        git(self.repo, "checkout", "-q", BRANCH)
        self.put("a.txt", "work\n", "card work")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.behind()
        result = self.check()
        self.assertEqual(result["verdict"], "wait")
        self.assertIn("is a merge", self.detail(result, "sync"))

    def test_a_card_behind_origin_main_is_replayed_onto_it_and_that_is_checked(self):
        card = self.put("a.txt", "work\n", "card work")
        self.behind()
        self.cfg["contract_commands"] = ["test -f a.txt && test -f b.txt"]   # needs both sides
        before = self.refs()
        result = self.check()
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.status(result)["sync"], "pass")
        new = result["commit"]
        self.assertNotEqual(new, card)
        self.assertEqual(result["card_commit"], card)
        self.assertEqual(git(self.repo, "rev-parse", f"{new}^"), result["base"])   # oracle 2
        self.assertEqual(git(self.repo, "show", f"{new}:a.txt"), "work")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s", new), "card work")
        self.assertEqual(self.refs(), before)     # objects only: no ref moved

    def test_a_card_commit_off_the_epic_branch_is_red(self):
        self.put("a.txt", "work\n", "card work")
        git(self.repo, "checkout", "-q", "-b", "other", "main")
        stray = self.put("c.txt", "stray\n", "stray")
        git(self.repo, "checkout", "-q", BRANCH)
        result = self.check(commit=stray)
        self.assertEqual(self.status(result)["sync"], "fail")


class FullReplay(World):
    """A reworked card is its original commit plus a fix-up, both named `(<task_id>, capsule run
    N)` as the runners write them: a replay onto a moved main carries both, oldest first."""
    TID, OTHER = "t_card0001", "t_other002"

    def msg(self, what, n=1, tid=None):
        return f"{what} ({tid or self.TID}, capsule run {n})"

    def card(self):
        first = self.put("a.txt", "work\n", self.msg("card work"))
        return first, self.put("a.txt", "work\nfix\n", self.msg("card fix-up", 2))

    def landed_then_edited_on_main(self):
        """The original, already on main by replay (as A'), then another card edits its lines
        there. Returns the original; the epic branch is left holding it, checked out."""
        first = self.put("a.txt", "one\ntwo\n", self.msg("card work"))
        git(self.repo, "checkout", "-q", "main")
        self.put("z.txt", "z\n", "main moved")
        git(self.repo, "cherry-pick", first)
        self.put("a.txt", "other\n", self.msg("other card", tid=self.OTHER))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        return first

    def test_an_original_its_last_landing_replayed_is_not_picked_again(self):
        first = self.landed_then_edited_on_main()
        fix = self.put("b.txt", "fix\n", self.msg("card fix-up", 2))
        result = self.check(task_id=self.TID, after=lambda: first)
        self.assertEqual((result["verdict"], result["card_commit"]), ("checked", fix))
        self.assertEqual(self.replayed(result), [self.msg("card fix-up", 2)])
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:a.txt"), "other")

    def test_without_the_floor_the_original_is_still_skipped_by_its_patch_id(self):
        self.landed_then_edited_on_main()
        self.put("b.txt", "fix\n", self.msg("card fix-up", 2))
        before = self.refs()
        result = self.check(task_id=self.TID)
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.replayed(result), [self.msg("card fix-up", 2)])
        self.assertEqual(self.refs(), before)

    def test_with_a_revert_on_main_the_equal_patch_is_not_skipped_and_the_wait_names_it(self):
        """The equal commit may be the one that was reverted: the change is wanted again."""
        first = self.put("a.txt", "one\ntwo\n", self.msg("card work"))
        git(self.repo, "checkout", "-q", "main")
        self.put("z.txt", "z\n", "main moved")     # so the copy is a new commit, not `first`
        git(self.repo, "cherry-pick", first)
        git(self.repo, "revert", "--no-edit", "HEAD")
        self.put("a.txt", "other\n", self.msg("other card", tid=self.OTHER))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        self.put("b.txt", "fix\n", self.msg("card fix-up", 2))
        result = self.check(task_id=self.TID)
        self.assertEqual(result["verdict"], "wait")
        self.assertIn(f"of {first[:12]} conflicts in a.txt", self.detail(result, "sync"))

    def test_a_conflict_whose_commit_git_cannot_name_leaves_it_out_of_the_detail(self):
        self.card()
        self.behind("a.txt")
        real = lander._git

        def failing(repo, *args, **kw):
            if args[0] == "rev-parse" and "CHERRY_PICK_HEAD" in args:
                return 128, "fatal: Needed a single revision"
            return real(repo, *args, **kw)

        with mock.patch.object(lander, "_git", failing):
            result = self.check(task_id=self.TID)
        detail = self.detail(result, "sync")
        self.assertEqual(result["verdict"], "wait")
        self.assertIn("replay conflicts in a.txt", detail)
        self.assertNotIn("fatal", detail)

    def test_a_commit_list_git_cannot_give_waits_and_never_falls_back_to_the_tip(self):
        self.card()
        self.behind()
        before = self.refs()
        real = lander._git

        def failing(repo, *args, **kw):
            if args[0] == "rev-list" and "--reverse" in args:
                return 128, "boom"
            return real(repo, *args, **kw)

        with mock.patch.object(lander, "_git", failing):
            result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_rebase"))
        self.assertIn("could not list the card's commits", self.detail(result, "sync"))
        self.assertEqual(self.refs(), before)

    def replayed(self, result):
        """Subjects of the commits the check ran on, oldest first, above the range base."""
        return git(self.repo, "log", "--reverse", "--format=%s",
                   f"{result['base']}..{result['commit']}").splitlines()

    def test_both_commits_of_a_reworked_card_replay_in_order_when_main_moved(self):
        first, second = self.card()
        self.behind()
        before = self.refs()
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["card_commit"]), ("checked", second))
        self.assertEqual(self.replayed(result), [self.msg("card work"), self.msg("card fix-up", 2)])
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:a.txt"), "work\nfix")
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:b.txt"), "main moved")
        self.assertEqual(git(self.repo, "rev-parse", f"{result['commit']}~2"), result["base"])
        self.assertEqual(self.refs(), before)

    def test_a_branch_that_holds_main_lands_both_commits_unreplayed(self):
        first, second = self.card()
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["commit"]), ("checked", second))
        self.assertEqual(self.replayed(result), [self.msg("card work"), self.msg("card fix-up", 2)])

    def test_another_cards_commit_between_the_two_is_not_replayed(self):
        self.put("a.txt", "work\n", self.msg("card work"))
        self.put("o.txt", "other card\n", self.msg("other card", tid=self.OTHER))
        self.put("a.txt", "work\nfix\n", self.msg("card fix-up", 2))
        self.behind()
        result = self.check(task_id=self.TID)
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.replayed(result), [self.msg("card work"), self.msg("card fix-up", 2)])
        self.assertNotIn("o.txt", git(self.repo, "ls-tree", "-r", "--name-only", result["commit"]))

    def main_again(self, text="work\n"):
        """Main holds the card's patch as someone else's commit, then changes those lines."""
        git(self.repo, "checkout", "-q", "main")
        self.put("a.txt", text, "the same patch, rebased")
        self.put("a.txt", text + "more\n", "main changed it again")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)

    def test_a_card_whose_patch_is_on_main_is_already_there_though_main_changed_it_again(self):
        self.put("a.txt", "work\n", self.msg("card work"))
        self.main_again()
        before = self.refs()
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["commit"]), ("checked", result["base"]))
        self.assertIn("already on origin/main", self.detail(result, "sync"))
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:a.txt"), "work\nmore")
        self.assertEqual(self.refs(), before)

    def test_only_the_commits_whose_patch_is_not_on_main_are_replayed(self):
        self.put("a.txt", "work\n", self.msg("card work"))
        self.put("c.txt", "new\n", self.msg("card more", 2))
        self.main_again()
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], self.replayed(result)),
                         ("checked", [self.msg("card more", 2)]))
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:a.txt"), "work\nmore")

    def test_a_patch_that_differs_from_main_only_in_whitespace_is_not_the_same_change(self):
        """`git cherry` ignores whitespace; an indented line is not the card's unindented one."""
        self.put("a.txt", "work\n", self.msg("card work"))
        self.main_again("    work\n")
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_rebase"))
        self.assertIn("conflicts in a.txt", self.detail(result, "sync"))

    def test_the_users_diff_config_does_not_change_which_patches_are_the_same(self):
        """`color.ui=always` would make equal patches differ, and a textconv that drops spaces
        would make the indented one equal: the ids ignore both."""
        for text, verdict in (("work\n", "checked"), ("    work\n", "wait")):
            self.setUp()
            git(self.repo, "config", "color.ui", "always")
            git(self.repo, "config", "diff.nospace.textconv", "sh -c 'tr -d \" \" < \"$0\"'")
            (self.repo / ".git" / "info" / "attributes").write_text("*.txt diff=nospace\n")
            self.put("a.txt", "work\n", self.msg("card work"))
            self.main_again(text)
            result = self.check(task_id=self.TID)
            self.assertEqual(result["verdict"], verdict, text)

    def test_a_skip_that_keeps_failing_is_tried_once_per_commit_then_the_replay_waits(self):
        self.put("a.txt", "work\n", self.msg("card work"))
        self.main_again()
        real, skips = lander._git, []

        def flaky(repo, *args, **kw):
            if "--skip" not in args:
                return real(repo, *args, **kw)
            skips.append(args)
            self.assertLess(len(skips), 6, "the skip loop is unbounded")
            return 1, "skip failed"
        with mock.patch.object(lander, "_git", flaky):
            result = self.check(task_id=self.TID)
        self.assertEqual((len(skips), result["verdict"]), (1, "wait"))

    def test_a_conflicting_patch_that_is_not_on_main_still_waits(self):
        self.put("a.txt", "work\n", self.msg("card work"))
        git(self.repo, "checkout", "-q", "main")
        self.put("a.txt", "something else\n", "another change")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_rebase"))

    def test_an_original_already_on_main_is_dropped_and_only_the_fix_up_lands(self):
        self.card()
        git(self.repo, "checkout", "-q", "main")
        self.put("a.txt", "work\n", "a human landed the original")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        result = self.check(task_id=self.TID)
        self.assertEqual(result["verdict"], "checked")
        self.assertEqual(self.replayed(result), [self.msg("card fix-up", 2)])
        self.assertEqual(git(self.repo, "show", f"{result['commit']}:a.txt"), "work\nfix")

    def test_a_conflicting_original_waits_and_nothing_is_pushed(self):
        self.card()
        self.behind("a.txt")
        before = self.refs()
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], result["hold"]), ("wait", "lander_wait_rebase"))
        self.assertIn("conflicts in a.txt", self.detail(result, "sync"))
        self.assertEqual(self.status(result)["suites"], "skipped")
        self.assertEqual(self.refs(), before)

    def test_no_commit_naming_the_card_falls_back_to_the_tip_alone(self):
        self.put("a.txt", "work\n", "card work")
        self.put("c.txt", "fix\n", "card fix-up")
        self.behind()
        result = self.check(task_id=self.TID)
        self.assertEqual((result["verdict"], self.replayed(result)), ("checked", ["card fix-up"]))

    def test_a_tip_whose_message_lost_the_id_still_goes_last(self):
        """The runner cuts a message at 500 characters, which can take the id with it."""
        self.put("a.txt", "work\n", self.msg("card work"))
        self.put("a.txt", "work\nfix\n", "card fix-up with a hand-edited message")
        self.behind()
        result = self.check(task_id=self.TID)
        self.assertEqual(self.replayed(result),
                         [self.msg("card work"), "card fix-up with a hand-edited message"])

    def test_a_very_long_title_keeps_the_whole_suffix_and_the_lander_still_matches_it(self):
        def committed(task, run_id):
            with mock.patch.object(workspace, "commit", return_value="sha") as commit, \
                    mock.patch.object(epic_sync, "txn"), mock.patch.object(epic_sync, "event"), \
                    mock.patch.object(epic_sync, "after_gate_pass", return_value=False):
                epic_sync.finish_gate_pass(mock.MagicMock(), None, {}, task, "ws", None, {"id": 1},
                                           {}, run_id, 0, False, [], [])
            return commit.call_args.args[1]

        message = committed({"id": self.TID, "title": "long title " * 80}, 7)
        self.assertLessEqual(len(message), 500)
        self.assertTrue(message.endswith(f" ({self.TID}, capsule run 7)"))
        self.assertEqual(committed({"id": "t_1", "title": "short"}, 2),
                         "short (t_1, capsule run 2)")
        base = git(self.repo, "rev-parse", "HEAD")
        first = self.put("a.txt", "work\n", message)
        tip = self.put("a.txt", "work\nfix\n", "tip without the id")
        self.assertEqual(lander._card_commits(self.repo, tip, base, self.TID), [first, tip])


class RedSuite(World):
    def test_a_failing_suite_is_red_and_carries_its_tail(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["contract_commands"] = ["echo one", "echo boom-line; exit 3"]
        before = self.refs()
        result = self.check()
        self.assertEqual(result["verdict"], "red")
        row = next(c for c in result["checks"] if c["name"] == "suites")
        self.assertEqual(row["status"], "fail")
        self.assertIn("boom-line", row["detail"])
        self.assertEqual(self.refs(), before)

    def test_a_failing_gate_is_red_and_the_suites_still_do_not_run(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["lander"]["gates"] = ["echo size-bad; exit 1"]
        result = self.check()
        self.assertEqual(self.status(result)["gates"], "fail")
        self.assertEqual(self.status(result)["suites"], "skipped")

    def test_gates_get_the_base_and_head_shas(self):
        head = self.put("a.txt", "work\n", "card work")
        self.cfg["lander"]["gates"] = ["echo {base} {head}"]
        result = self.check()
        row = next(c for c in result["checks"] if c["name"] == "gates")
        self.assertIn(f"{git(self.repo, 'rev-parse', 'origin/main')} {head}", row["detail"])

    def test_a_timeout_is_red(self):
        self.put("a.txt", "work\n", "card work")
        self.cfg["contract_commands"] = ["sleep 5"]
        result = self.check(timeout=0.3)
        self.assertEqual(self.status(result)["suites"], "fail")
        self.assertIn("timed out", self.detail(result, "suites"))


class SuitesDetail(World):
    """Z1 (contracts/intent/lander.jsonl, t_51c38ce5): the suites detail names the failing test,
    and a red suites check runs once more. Oracle 2 reads the marker file the command leaves."""

    def run_suites(self, case):
        self.put("a.txt", "work\n", "card work")
        seen = self.dir / "seen"
        noise = "for i in $(seq 80); do echo lookout-noise-$i; done"
        fail = f"echo 'FAIL: test_x (m.C)'; {noise}; echo run >> {seen}; exit 1"
        flaky = f"test -f {seen}.1 || {{ touch {seen}.1; {fail}; }}; echo OK"
        self.cfg["contract_commands"] = [flaky if case == "suites_flaky" else fail]
        result = self.check()
        runs = len(seen.read_text().split()) if seen.exists() else 0
        return self.status(result)["suites"], self.detail(result, "suites"), runs

    def test_the_failing_test_line_survives_the_noise(self):
        status, detail, _ = self.run_suites("suites_red")
        self.assertEqual(status, "fail")
        self.assertIn("FAIL: test_x (m.C)", detail)
        self.assertLessEqual(len(detail), lander.DETAIL)

    def test_red_then_green_is_flaky_and_passes(self):
        status, detail, runs = self.run_suites("suites_flaky")
        self.assertEqual((status, runs), ("pass", 1))
        self.assertIn("red, then green on retry (flaky)", detail)
        self.assertIn("FAIL: test_x (m.C)", detail)

    def test_red_twice_is_red_after_exactly_two_runs(self):
        status, detail, runs = self.run_suites("suites_red")
        self.assertEqual((status, runs), ("fail", 2))
        self.assertNotIn("flaky", detail)

    def test_a_timed_out_suite_is_not_re_run(self):
        self.put("a.txt", "work\n", "card work")
        seen = self.dir / "seen"
        self.cfg["contract_commands"] = [f"echo run >> {seen}; sleep 5"]
        result = self.check(timeout=0.5)
        self.assertEqual(self.status(result)["suites"], "fail")
        self.assertEqual(len(seen.read_text().split()), 1)

    def test_the_shipped_examples(self):
        shipped = [e for e in intent.load("lander") if "suites" in e["expect"]]
        self.assertEqual(len(shipped), 3)
        for ex in shipped:
            self.setUp()
            status, detail, _ = self.run_suites(ex["given"]["document"]["case"])
            want = ex["expect"]
            self.assertEqual(status, want["suites"], ex["id"])
            self.assertIn(want["detail"], detail, ex["id"])


class StaleMarker(World):
    def marker_commit(self, sha):
        text = (f"# mutation-check: targets-predate-range {sha} -- the target predates the range"
                " :: Case.test_a\nx = 1\n")
        return self.put("tests/test_m.py", text, "marker")

    def test_a_real_shaped_marker_naming_an_older_sha_is_held_not_red(self):
        old = self.put("old.txt", "old\n", "old")
        git(self.repo, "checkout", "-q", "main")
        self.put("b.txt", "main moved\n", "main moved")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "main")
        self.marker_commit(old)
        result = self.check()
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(self.status(result)["stale_marker"], "hold")
        self.assertEqual(result["hold"], "stale_marker")

    def test_a_marker_at_an_older_main_commit_passes_after_main_moves(self):
        older = git(self.repo, "rev-parse", "origin/main")
        git(self.repo, "checkout", "-q", "main")
        self.put("b.txt", "main moved\n", "main moved")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "rebase", "-q", "main")
        self.marker_commit(older)
        self.assertEqual(self.check()["verdict"], "checked")

    def test_a_marker_naming_a_sha_inside_the_range_is_held(self):
        inside = self.put("a.txt", "work\n", "card work")
        self.marker_commit(inside)
        self.assertEqual(self.check()["verdict"], "held")

    def test_a_marker_naming_the_range_base_is_valid(self):
        self.marker_commit(git(self.repo, "rev-parse", "origin/main"))
        self.assertEqual(self.check()["verdict"], "checked")


class Protected(World):
    def test_a_protected_path_holds_for_the_stamp_even_when_everything_else_is_green(self):
        self.put("docs/ZENKAI.md", "changed\n", "touch zenkai")
        result = self.check()
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(result["hold"], "protected")
        self.assertEqual(self.status(result)["suites"], "pass")
        self.assertIn("docs/ZENKAI.md", self.detail(result, "protected"))

    def test_a_rename_out_of_a_protected_path_is_held(self):
        self.put("contracts/intent/a.jsonl", "{}\n", "seed")
        git(self.repo, "push", "-q", "origin", f"{BRANCH}:main")
        git(self.repo, "mv", "contracts/intent/a.jsonl", "a.jsonl")
        git(self.repo, "mv", "README.md", "docs_readme.md")
        git(self.repo, "commit", "-qm", "rename out")
        result = self.check()
        self.assertEqual(result["verdict"], "held")
        self.assertIn("contracts/intent/a.jsonl", self.detail(result, "protected"))

    def test_renaming_zenkai_itself_is_held(self):
        self.put("docs/ZENKAI.md", "z\n", "seed")
        git(self.repo, "push", "-q", "origin", f"{BRANCH}:main")
        git(self.repo, "mv", "docs/ZENKAI.md", "docs/OLD.md")
        git(self.repo, "commit", "-qm", "rename")
        self.assertEqual(self.check()["verdict"], "held")

    def test_case_variants_of_protected_paths_are_held(self):
        for path in ("Contracts/intent/x.jsonl", "Capsule/Intent.py", "docs/zenkai.md"):
            with self.subTest(path=path):
                self.setUp()
                self.put(path, "x\n", "case variant")
                result = self.check()
                self.assertEqual(result["verdict"], "held")
                self.assertEqual(result["hold"], "protected")

    def test_the_worker_write_fence_sees_case_variants_too(self):
        got = workspace.touches_protected(["docs/zenkai.md", "CAPSULE/intent.py", "docs/other.md"])
        self.assertEqual(got, ["docs/zenkai.md", "CAPSULE/intent.py"])

    def test_two_paths_that_differ_only_in_case_are_held(self):
        for name, text in (("Readme.txt", "a\n"), ("README.txt", "b\n")):   # plumbing: APFS
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(self.repo),
                                  input=text, capture_output=True, text=True).stdout.strip()
            git(self.repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},notes/{name}")
        git(self.repo, "commit", "-qm", "collide")
        result = self.check()
        self.assertEqual(result["verdict"], "held")
        self.assertIn("case-colliding", self.detail(result, "protected"))

    def test_a_non_ascii_protected_path_is_held(self):
        self.put("contracts/intent/évil name.jsonl", "{}\n", "odd")
        result = self.check()
        self.assertEqual(result["verdict"], "held")
        self.assertIn("évil name.jsonl", self.detail(result, "protected"))

    def test_a_non_ascii_protected_path_with_a_matching_sums_line_is_held(self):
        name = "évil.jsonl"
        self.sums(name, "{}\n", sha256("{}\n"))
        self.assertEqual(self.check()["verdict"], "held")

    def sums(self, name, text, stated):
        self.put(f"contracts/intent/{name}", text, "intent")
        self.put("contracts/intent/SHA256SUMS", f"{stated}  {name}\n", "sums")

    def test_a_sums_line_matching_the_file_is_held_not_red(self):
        self.sums("x.jsonl", "{}\n", sha256("{}\n"))
        self.assertEqual(self.check()["verdict"], "held")

    def test_a_sums_line_that_does_not_match_is_red(self):
        self.sums("x.jsonl", "{}\n", sha256("other"))
        result = self.check()
        self.assertEqual(result["verdict"], "red")
        self.assertEqual(self.status(result)["protected"], "fail")

    def test_a_changed_intent_file_with_no_sums_line_is_red(self):
        self.put("contracts/intent/SHA256SUMS", "", "sums")
        self.put("contracts/intent/y.jsonl", "{}\n", "unlisted")
        self.assertEqual(self.status(self.check())["protected"], "fail")


class Record(World):
    def setUp(self):
        super().setUp()
        self.put("a.txt", "work\n", "card work")
        board = self.dir / "board.db"
        db.create_board(board)
        self.conn = db.connect(board)
        self.addCleanup(self.conn.close)
        self.tid = db.new_task(self.conn, title="t", assignee="dev", branch=BRANCH, status="done")

    def state(self):
        row = self.one("SELECT status, failure_count FROM tasks WHERE id=?")
        events = self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))
        return tuple(row), [r[0] for r in events]

    def one(self, sql):
        return self.conn.execute(sql, (self.tid,)).fetchone()

    def test_a_flaky_suite_records_a_landing_flaky_event(self):
        seen = self.dir / "seen"
        self.cfg["contract_commands"] = [f"test -f {seen} || {{ touch {seen}; exit 1; }}"]
        lander.record(self.conn, self.tid, self.check(), limit=3, now=5)
        (status, fails), kinds = self.state()
        (ex,) = [e for e in intent.load("lander") if "flaky_event" in e["expect"]]
        self.assertEqual(kinds.count("landing_flaky"), ex["expect"]["flaky_event"])
        self.assertEqual((status, fails), ("done", 0))
        row = self.conn.execute("SELECT payload FROM task_events WHERE kind='landing_flaky'")
        self.assertIn("red, then green on retry", json.loads(row.fetchone()[0])["detail"])

    def test_green_records_only_the_event(self):
        lander.record(self.conn, self.tid, self.check(), limit=3, now=5)
        (status, fails), kinds = self.state()
        self.assertEqual((status, fails), ("done", 0))
        self.assertIn("landing_checked", kinds)
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing_checked'").fetchone()[0])
        self.assertEqual(payload["verdict"], "checked")
        self.assertEqual(len(payload["checks"]), 7)

    def test_red_sends_the_card_back_to_ready_with_the_suite_tail(self):
        self.cfg["contract_commands"] = ["echo red-tail; exit 1"]
        lander.record(self.conn, self.tid, self.check(), limit=3, now=5)
        (status, fails), _ = self.state()
        self.assertEqual((status, fails), ("ready", 1))
        reason = self.one("SELECT last_failure_error FROM tasks WHERE id=?")[0]
        self.assertIn("red-tail", reason)

    def test_held_blocks_the_card(self):
        self.put("docs/ZENKAI.md", "x\n", "protected")
        lander.record(self.conn, self.tid, self.check(), limit=3, now=5)
        row = self.one("SELECT status, block_kind FROM tasks WHERE id=?")
        self.assertEqual(tuple(row), ("blocked", "needs_input"))

    def test_a_branch_behind_origin_main_waits_and_is_never_a_counted_failure(self):
        self.put("a.txt", "card side\n", "card edit")
        self.behind("a.txt")      # a replay conflict
        lander.record(self.conn, self.tid, self.check(), limit=3, now=5)
        row = self.one("SELECT status, block_kind, failure_count FROM tasks WHERE id=?")
        self.assertEqual(tuple(row), ("blocked", "transient", 0))
        why = self.one("SELECT payload FROM task_events WHERE kind='blocked' AND task_id=?")[0]
        self.assertIn("needs rebase onto origin/main", why)

    def test_the_event_and_the_move_are_one_transaction(self):
        self.put("docs/ZENKAI.md", "x\n", "protected")
        with db.txn(self.conn):
            self.conn.execute("CREATE TRIGGER no_block BEFORE UPDATE OF block_kind ON tasks "
                              "BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with self.assertRaises(Exception):
            lander.record(self.conn, self.tid, self.check(), limit=3, now=5)
        (status, fails), kinds = self.state()
        self.assertEqual((status, fails), ("done", 0))
        self.assertNotIn("landing_checked", kinds)


class IntentExamples(World):
    """Z1: contracts/intent/lander.jsonl replayed through the real `lander.run`."""

    DOCKER_CASES = {"docker_path": "docker/Dockerfile", "pg_script": "scripts/backup_pg.sh",
                    "observability_path": "observability/sub/x.yaml",
                    "maintenance_sql": "maintenance/migrations/0001.sql"}

    def replay(self, doc):
        marker = self.dir / "host-marker"    # a suite command that ran on the host leaves it
        if doc["case"] == "protected_sums":
            self.put("contracts/intent/x.jsonl", "{}\n", "intent")
            self.put("contracts/intent/SHA256SUMS", f"{sha256('{}' + chr(10))}  x.jsonl\n", "sums")
        elif doc["case"] == "protected":
            self.put("docs/ZENKAI.md", "x\n", "protected")
        elif doc["case"] == "behind_main":
            self.put("a.txt", "w\n", "work")
            self.behind("a.txt")
        elif doc["case"] in self.DOCKER_CASES:
            self.put(self.DOCKER_CASES[doc["case"]], "x\n", "work")
            self.cfg["contract_commands"] = [f"echo ran > {marker}"]
        else:
            self.put("a.txt", "w\n", "work")
        if doc["case"] == "red":
            self.cfg["contract_commands"] = ["exit 1"]
        if doc["case"] == "docker":
            self.cfg["lander"]["needs_docker"] = self.cfg["contract_commands"]
        before = self.refs()
        result = self.check()
        return {"verdict": result["verdict"],
                "detail": "refs unmoved" if self.refs() == before else "refs moved",
                "hold": result["hold"],
                "host_marker": "present" if marker.exists() else "absent"}

    def test_the_shipped_examples(self):
        every = intent.load("lander")
        examples = [e for e in every if "verdict" in e["expect"]]
        self.assertEqual(len(examples), 8)
        # C07b outcome (test_landing), C07d step (test_landing_step), G3 mutex (test_lander_guard),
        # W205 replay (test_landing), day-branch rows (2)
        self.assertEqual(len(every) - len(examples), 20)
        seen = set()
        for ex in examples:
            seen.add(ex["id"])
            self.setUp()
            got = self.replay(ex["given"]["document"])
            self.assertLessEqual(set(ex["expect"]), set(got), ex["id"])   # no unchecked key
            for key, want in ex["expect"].items():
                self.assertEqual(got[key], want, f"{ex['id']}: {key}")
        self.assertEqual(len(seen), 8)


class DockerAuto(World):
    """The lander pins CAPSULE_DOCKER_AUTO_BASE, so the shell runner opts into docker by diff."""

    def probe(self, path, inherit=None):
        marker = self.dir / "docker-seen"
        git(self.repo, "checkout", "-q", "main")
        self.put("tests/run_shell_suites.sh",
                 (Path(__file__).resolve().parents[2] / "tests/run_shell_suites.sh").read_text(),
                 "runner")
        self.put("tests/test_probe.sh", f'echo "${{RUN_DOCKER_TESTS:-off}}" > {marker}\n', "probe")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        git(self.repo, "reset", "-q", "--hard", "main")
        self.put(path, "x\n", "card work")
        touched = git(self.repo, "diff", "--name-only", "main...HEAD").split()   # second oracle
        self.docker_diff = any(re.match(r"(docker|observability)/", f) for f in touched)
        self.cfg["contract_commands"] = ["bash tests/run_shell_suites.sh"]
        with mock.patch.dict(os.environ, inherit or {}):
            self.verdict = self.check()["verdict"]
        return marker.read_text().strip() if marker.exists() else "not run"

    def test_a_docker_path_diff_holds_the_card_and_runs_no_suite(self):
        """Row 632 b: the sandbox has no docker, so the docker tests are the keeper's."""
        self.assertEqual(self.probe("docker/Dockerfile"), "not run")
        self.assertTrue(self.docker_diff)
        self.assertEqual(self.verdict, "held")

    def test_a_plain_diff_leaves_them_off(self):
        self.assertEqual(self.probe("a.txt"), "off")
        self.assertFalse(self.docker_diff)
        self.assertEqual(self.verdict, "checked")

    def test_a_clone_without_a_base_never_inherits_one(self):
        """landing._suites_red clones with no base: neither env may carry the parent's value."""
        with mock.patch.dict(os.environ, {"CAPSULE_DOCKER_AUTO_BASE": "HEAD~5"}):
            with lander._clone(self.repo, "HEAD", True) as work:
                self.assertNotIn("CAPSULE_DOCKER_AUTO_BASE", work.env)
                self.assertNotIn("CAPSULE_DOCKER_AUTO_BASE", work.deploy_env)


if __name__ == "__main__":
    unittest.main()
