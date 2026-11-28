"""LAND-03 (row 600): a product board lands on `lander.branch`, `pr daily` opens one PR a day.
The GitHub CLI is a fake script that logs its argv and stdin; pushes go to a temp bare remote."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import cli_ops, config, daily_pr, db, lander, paths
from capsule.db import event
from capsule.tests.test_lander import BRANCH, World, git
from capsule.tests.test_landing import Landing

FAKE = """#!/bin/sh
{ printf '%s' "$*" | tr '\\n' '|'; echo; } >> "$FAKE_LOG"
[ -f "$FAKE_LOG.fail" ] && { echo "gh: boom" >&2; exit 1; }
if [ "$1 $2" = "pr list" ]; then cat "$FAKE_LOG.open" 2>/dev/null || echo '[]'; exit 0; fi
cat > "$FAKE_LOG.body"
echo "https://github.com/o/r/pull/7"
"""
REPO = "https://github.com/o/r.git"
WHO = ("-c", "user.name=t", "-c", "user.email=t@t")


class Branch(unittest.TestCase):
    def test_lander_target_defaults_to_main(self):
        self.assertEqual(lander.target({}), "main")
        self.assertEqual(lander.target({"lander": {"mode": "land"}}), "main")
        self.assertEqual(lander.target({"lander": {"branch": "capsule/daily"}}), "capsule/daily")

    def test_valid_names_pass_and_main_aliases_or_bad_refs_are_refused(self):
        for ok in ("main", "capsule/daily", "release-1.2", "a/b/c"):
            config.validate_lander_branch({"branch": ok})
        config.validate_lander_branch(None)
        for bad in ("refs/heads/main", "origin/main", "heads/main", "refs/remotes/origin/main",
                    "remotes/origin/x", "origin/x", "refs/heads/x", "HEAD", "heads/x",
                    "", "a..b", "a b", "-x", "x/", "x.lock", "a@{b", "/x", "a~1", "@", 7, None):
            with self.assertRaisesRegex(ValueError, "lander.branch"):
                config.validate_lander_branch({"branch": bad})

    def test_board_new_writes_the_day_branch_except_for_capsule_corp(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        for name in ("acme", "capsule-corp"):
            r = subprocess.run([sys.executable, "-m", "capsule", "board", "new", name, "--repo",
                                REPO, "--capsule-home", str(home)], cwd=paths.REPO,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        acme = json.loads((home / "boards/acme/config.json").read_text())
        corp = json.loads((home / "boards/capsule-corp/config.json").read_text())
        self.assertEqual(acme["lander"]["branch"], "capsule/daily")
        self.assertEqual(lander.target(config.load(home / "boards/acme/config.json")),
                         "capsule/daily")
        self.assertEqual(lander.target(corp), "main")
        self.assertTrue(cli_ops._new_config("x", REPO)["lander"]["branch"])


def remote_has(remote, name):
    return subprocess.run(["git", "-C", str(remote), "rev-parse", "--verify", "-q",
                           f"refs/heads/{name}"], capture_output=True, text=True).stdout.strip()


def is_ancestor(remote, older, newer):
    return subprocess.run(["git", "-C", str(remote), "merge-base", "--is-ancestor", older,
                           newer]).returncode == 0


class Pushes(Landing):
    def setUp(self):
        super().setUp()
        self.cfg["lander"]["branch"] = "capsule/daily"

    def test_a_card_lands_on_a_branch_origin_does_not_have_yet_and_main_never_moves(self):
        self.assertEqual(remote_has(self.remote, "capsule/daily"), "")      # not created first
        head = self.put("a.txt", "work\n", "card work")
        main_before = git(self.remote, "rev-parse", "main")
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        self.assertEqual(remote_has(self.remote, "capsule/daily"), head)
        self.assertEqual(git(self.remote, "rev-parse", "main"), main_before)

    def test_a_branch_behind_main_is_caught_up_with_a_merge_and_the_push_fast_forwards(self):
        git(self.remote, "branch", "capsule/daily", "main")
        old = remote_has(self.remote, "capsule/daily")
        self.behind("b.txt")
        main_now = git(self.remote, "rev-parse", "main")
        self.put("a.txt", "work\n", "card work")
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        tip = remote_has(self.remote, "capsule/daily")
        self.assertTrue(is_ancestor(self.remote, old, tip))
        self.assertTrue(is_ancestor(self.remote, main_now, tip))
        self.assertEqual(git(self.remote, "rev-parse", "main"), main_now)
        files = git(self.remote, "ls-tree", "--name-only", tip)
        self.assertTrue("a.txt" in files and "b.txt" in files)

    def test_a_branch_deleted_on_origin_is_not_resurrected_from_a_stale_tracking_ref(self):
        git(self.repo, "checkout", "-q", "-b", "dayb", "main")
        self.put("x.txt", "day\n", "day work")
        git(self.repo, "push", "-q", "origin", "dayb:refs/heads/capsule/daily")
        git(self.repo, "fetch", "-q", "origin")                     # the tracking ref exists
        git(self.remote, "branch", "-D", "capsule/daily")           # merged and deleted
        git(self.repo, "checkout", "-q", "main")
        self.put("x.txt", "main\n", "main squashed the day")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        self.put("a.txt", "work\n", "card work")
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("landed", True))
        tip = remote_has(self.remote, "capsule/daily")     # replayed onto main, not the dead branch
        self.assertEqual(git(self.remote, "rev-parse", f"{tip}~1"),
                         git(self.remote, "rev-parse", "main"))
        self.assertEqual(git(self.remote, "show", f"{tip}:x.txt"), "main")


class Conflict(World):
    def test_a_conflicting_catch_up_waits_naming_the_branch(self):
        git(self.repo, "checkout", "-q", "-b", "dayb", "main")
        self.put("x.txt", "day\n", "day work")
        git(self.repo, "push", "-q", "origin", "dayb:refs/heads/capsule/daily")
        git(self.repo, "checkout", "-q", "main")
        self.put("x.txt", "main\n", "main work")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        self.put("a.txt", "work\n", "card work")
        self.cfg["lander"]["branch"] = "capsule/daily"
        result = self.check()
        self.assertEqual(result["verdict"], "wait")
        self.assertEqual([c["name"] for c in result["checks"]], ["fetch", "sync"])
        self.assertIn("origin/capsule/daily", self.detail(result, "sync"))
        self.assertNotIn("origin/main", self.detail(result, "sync"))
        self.assertEqual(remote_has(self.remote, "capsule/daily"), git(self.repo, "rev-parse",
                                                                       "dayb"))


class PrDaily(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home, self.log = self.dir / "home", self.dir / "gh.log"
        self.gh = self.dir / "gh"
        self.gh.write_text(FAKE)
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, CAPSULE_GH_BIN=str(self.gh), FAKE_LOG=str(self.log))
        self.remote, self.repo = self.dir / "remote.git", self.dir / "repo"
        git(self.dir, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(self.dir, "clone", "-q", str(self.remote), str(self.repo))
        git(self.repo, "checkout", "-q", "-B", "main")
        self.commit("start")
        git(self.repo, "push", "-q", "origin", "main")
        self.start = git(self.repo, "rev-parse", "HEAD")
        self.cfg(branch="capsule/daily")
        db.create_board(paths.board_db("b", self.home))
        self.conn = db.connect(paths.board_db("b", self.home))
        self.addCleanup(self.conn.close)

    def commit(self, msg):
        git(self.repo, *WHO, "commit", "-q", "--allow-empty", "-m", msg)

    def cfg(self, repo=None, **lander):
        path = paths.config_path(self.home, "b")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"board": "b", "repo_url": repo or str(self.repo),
                                    "lander": lander}))

    def card(self, title, on_branch=True, outcome="landed", at=None, detail=""):
        """A task, its `landing` event and (on the branch) a commit naming it."""
        tid = db.new_task(self.conn, title=title, assignee="dev", branch="x", status="done")
        event(self.conn, tid, "landing", {"outcome": outcome, "commit": "c", "detail": detail},
              now=at)
        if on_branch:
            self.commit(f"{title} ({tid})")
            git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/capsule/daily")
        return tid

    def pr(self, *flags, board="b"):
        r = subprocess.run([sys.executable, "-m", "capsule", "pr", "daily", board, *flags,
                            "--capsule-home", str(self.home)], cwd=paths.REPO, env=self.env,
                           capture_output=True, text=True, timeout=60)
        return r, (self.log.read_text().splitlines() if self.log.exists() else [])

    def body(self):
        return Path(f"{self.log}.body").read_text()

    def test_landed_skips_reverted_and_old_cards_and_counts_a_recorded_after_kill(self):
        old = self.card("Old", at=100, on_branch=False)
        keep = self.card("Keep", at=200, on_branch=False)
        gone = self.card("Gone", at=200, on_branch=False)
        killed = self.card("Killed", at=250, on_branch=False, outcome="already_on_main",
                           detail="recorded after a kill")
        self.card("Other", at=250, on_branch=False, outcome="already_on_main")
        event(self.conn, gone, "landing", {"outcome": "reverted"}, now=300)
        self.assertEqual(daily_pr._landed(self.conn, 150), {keep: "Keep", killed: "Killed"})
        self.assertIn(old, daily_pr._landed(self.conn, 0))

    def test_main_board_prints_a_line_and_never_calls_the_cli(self):
        self.cfg()
        r, calls = self.pr()
        self.assertEqual((r.returncode, calls), (0, []))
        self.assertIn("main", r.stdout)

    def test_bad_branch_is_a_config_error_naming_the_key(self):
        self.cfg(branch="origin/main")
        r, calls = self.pr()
        self.assertEqual((r.returncode, calls), (1, []))
        self.assertIn("lander.branch", r.stderr)

    def test_an_unknown_board_exits_one_naming_it_and_creates_nothing(self):
        r, calls = self.pr(board="ghost")
        self.assertEqual((r.returncode, calls), (1, []))
        self.assertIn("ghost", r.stderr)
        self.assertEqual(sorted(p.name for p in (self.home / "boards").iterdir()), ["b"])

    def test_a_fresh_board_with_no_database_works(self):
        self.conn.close()
        for side in (self.home / "boards/b").glob("kanban.db*"):
            side.unlink()
        r, calls = self.pr()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to propose", r.stdout)

    def test_a_repo_that_is_neither_github_nor_a_directory_runs_nothing(self):
        self.cfg(repo="https://gitlab.example/o/r.git", branch="capsule/daily")
        r, calls = self.pr()
        self.assertEqual((r.returncode, calls), (1, []))

    def test_none_open_creates_a_draft_listing_the_git_cards_with_the_body_on_stdin(self):
        a, b = self.card("First card"), self.card("Second card")
        r, calls = self.pr()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("--head capsule/daily", calls[0])
        for want in ("pr create", "--draft", "--base main", "--head capsule/daily",
                     "--body-file -", f"Capsule daily: b {time.strftime('%Y-%m-%d')}"):
            self.assertIn(want, calls[1])
        self.assertNotIn("--force", calls[1])
        self.assertEqual(self.body(), f"- {a}: First card\n- {b}: Second card")
        self.assertEqual(r.stderr, "")

    def test_an_id_that_main_already_names_is_left_out_and_each_card_is_listed_once(self):
        a, b = self.card("First card"), self.card("Second card")
        self.commit(f"more on {b}")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/capsule/daily")
        git(self.repo, "checkout", "-q", "-b", "squash", self.start)
        self.commit(f"squash of the day ({a})")
        git(self.repo, "push", "-q", "origin", "squash:refs/heads/main")
        r, _ = self.pr()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.body(), f"- {b}: Second card")

    def test_a_revert_commit_on_main_does_not_hide_a_card_re_landed_on_the_branch(self):
        a = self.card("First card")
        git(self.repo, "checkout", "-q", "-b", "rev", self.start)
        self.commit(f'Revert "First card ({a})"\n\nThis reverts commit 0123abc, for {a}.')
        git(self.repo, "push", "-q", "origin", "rev:refs/heads/main")
        r, _ = self.pr()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.body(), f"- {a}: First card")

    def test_a_url_board_whose_clone_fails_exits_one_and_changes_nothing(self):
        self.cfg(repo="https://github.com/nobody/none.git", branch="capsule/daily")
        self.env["GIT_ALLOW_PROTOCOL"] = "file"                     # no network, ever
        r, calls = self.pr()
        self.assertEqual((r.returncode, calls), (1, []))
        self.assertIn("git clone failed", r.stderr)

    def test_a_failing_git_log_exits_one_and_never_blanks_an_open_pr(self):
        self.card("Card")
        Path(f"{self.log}.open").write_text('[{"number": 7, "url": "u"}]')
        real = lander._git

        def broken(repo, *args, **kw):
            return (128, "boom") if args[0] == "log" else real(repo, *args, **kw)
        with mock.patch.object(lander, "_git", broken):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                daily_pr._git_cards(str(self.repo), "capsule/daily")
            with mock.patch.dict(os.environ, self.env):
                self.assertEqual(daily_pr.run("b", self.home), 1)
        self.assertFalse(self.log.exists())                         # gh was never called

    def test_a_git_card_without_a_landed_event_and_the_reverse_warn_and_git_wins(self):
        a = self.card("Has event")
        ghost = self.card("No event", outcome="push_rejected")
        lost = self.card("Landed not in git", on_branch=False, at=int(time.time()) + 5)
        r, _ = self.pr()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{ghost} is on capsule/daily but has no landed event", r.stderr)
        self.assertIn(f"{lost} landed but is not in git", r.stderr)
        self.assertNotIn(a, r.stderr)
        self.assertIn(ghost, self.body())
        self.assertNotIn(lost, self.body())

    def test_the_mark_is_taken_before_the_query(self):
        self.card("Card")
        before = int(time.time())
        self.assertEqual(self.pr()[0].returncode, 0)
        mark = db.guard_get(self.conn, daily_pr.GUARD)[0]["at"]
        self.assertTrue(before - 1 <= mark <= int(time.time()))

    def test_nothing_on_the_branch_and_none_open_prints_nothing_to_propose(self):
        r, calls = self.pr()
        self.assertIn("nothing to propose", r.stdout)
        self.assertEqual(len(calls), 1)

    def test_one_open_is_edited_with_the_same_list_and_nothing_is_created(self):
        a = self.card("Card")
        Path(f"{self.log}.open").write_text('[{"number": 7, "url": "u"}]')
        r, calls = self.pr()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(calls[1].startswith("pr edit 7 --body-file -"))
        self.assertFalse(any("pr create" in c for c in calls))
        self.assertEqual(self.body(), f"- {a}: Card")

    def test_dry_run_prints_the_command_and_only_looks(self):
        self.card("Card")
        r, calls = self.pr("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn("pr create", r.stdout)
        self.assertIsNone(db.guard_get(self.conn, daily_pr.GUARD)[0])

    def test_a_cli_failure_exits_one_with_its_message_and_records_nothing(self):
        self.card("Card")
        Path(f"{self.log}.fail").write_text("")
        r, _ = self.pr()
        self.assertEqual(r.returncode, 1)
        self.assertIn("boom", r.stderr)
        self.assertIsNone(db.guard_get(self.conn, daily_pr.GUARD)[0])


if __name__ == "__main__":
    unittest.main()
