"""C07b fix round 1 (t_0fc44cdb): the red-after-push revert and the push-failure back-off. Same
real git over a temp bare remote as test_landing; nothing is mocked and the real origin is never
touched."""
import os
import stat
import unittest

from capsule import db, landing
from capsule.tests.test_landing import NOW, Landing, git


class RedAfterPush(Landing):
    def landed(self):
        self.put("a.txt", "work\n", "card work")
        sha = self.put("b.txt", "more\n", "more work")
        self.land()
        return sha

    def verify(self, **kw):
        return landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release,
                              now=NOW + 5, **kw)

    def tree(self):
        return git(self.remote, "ls-tree", "-r", "--name-only", "main").split()

    def incidents(self):
        return self.conn.execute("SELECT kind, task_id, evidence FROM incidents").fetchall()

    def test_a_green_verification_closes_the_watch(self):
        self.landed()
        self.assertEqual(self.verify()["outcome"], "verified")
        self.assertEqual(self.verify()["outcome"], "nothing")

    def test_red_pushes_revert_commits_keeps_history_and_opens_one_incident(self):
        sha = self.landed()
        self.cfg["contract_commands"] = ["echo boom-after-push; exit 1"]
        self.assertEqual(self.verify()["outcome"], "reverted")
        git(self.remote, "merge-base", "--is-ancestor", sha, "main")   # history kept, no reset
        self.assertEqual(self.tree(), ["README.md"])
        self.assertIn("Revert", git(self.remote, "log", "-1", "--format=%s", "main"))
        (incident,) = self.incidents()
        self.assertEqual((incident["kind"], incident["task_id"]), ("escaped_bug", self.tid))
        self.assertIn("boom-after-push", incident["evidence"])
        self.assertEqual(self.verify()["outcome"], "nothing")   # one revert only

    def test_a_ci_red_signal_reverts_without_running_the_suites(self):
        self.landed()
        self.cfg["contract_commands"] = ["exit 7"]   # would fail, but the signal is what counts
        self.assertEqual(self.verify(red="CI failed on main")["outcome"], "reverted")

    def test_only_the_cards_own_commits_are_reverted_not_another_authors(self):
        hotfix = self.rival("hotfix.txt")      # on origin; the release checkout is behind it
        git(self.repo, "fetch", "-q", "origin", "main")
        git(self.repo, "rebase", "-q", "origin/main")
        self.put("a.txt", "work\n", "card work")
        self.assertEqual(self.land()["outcome"], "landed")
        self.cfg["contract_commands"] = ["exit 1"]
        self.assertEqual(self.verify()["outcome"], "reverted")
        self.assertEqual(self.tree(), ["README.md", "hotfix.txt"])
        git(self.remote, "merge-base", "--is-ancestor", hotfix, "main")

    def reject_pushes(self, on):
        flag = self.dir / "reject"
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(f'#!/bin/sh\n[ -e "{flag}" ] && {{ echo "! [rejected] x" >&2; exit 1; }}\n'
                        "exit 0\n")
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
        flag.write_text("") if on else flag.unlink(missing_ok=True)

    def test_a_rejected_revert_push_leaves_the_release_alone_and_the_next_landing_free(self):
        sha = self.landed()
        release_head = git(self.release, "rev-parse", "HEAD")
        self.cfg["contract_commands"] = ["exit 1"]
        self.reject_pushes(True)
        self.assertEqual(self.verify()["outcome"], "revert_push_rejected")
        self.assertEqual(git(self.release, "rev-parse", "HEAD"), release_head)
        self.assertEqual(git(self.release, "status", "--porcelain"), "")
        self.assertEqual(self.remote_main(), sha)
        self.reject_pushes(False)
        self.assertEqual(self.verify()["outcome"], "reverted")      # the retry: one revert set
        self.assertEqual(self.tree(), ["README.md"])
        self.assertEqual(len(self.incidents()), 1)
        git(self.repo, "fetch", "-q", "origin", "main")             # a later card lands normally
        git(self.repo, "checkout", "-q", "-b", "epic/y", "origin/main")
        self.tid = db.new_task(self.conn, title="n", assignee="dev", branch="epic/y",
                               status="review")
        head = self.put("c.txt", "next\n", "next card")
        self.cfg["contract_commands"] = ["echo ok"]
        self.assertEqual(self.land(fetch=True)["outcome"], "landed")
        self.assertEqual(self.remote_main(), head)


class AlreadyOnMain(Landing):
    def status(self):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?", (self.tid,)).fetchone()[0]

    def test_every_verdict_leaves_a_landing_checked_event_even_in_dry_run(self):
        self.cfg["lander"]["mode"] = "dry-run"
        self.put("a.txt", "work\n", "card work")
        self.land()
        self.assertEqual(self.kinds().count("landing_checked"), 1)

    def test_a_sha_already_on_origin_takes_its_commits_from_the_earlier_check(self):
        self.cfg["lander"]["mode"] = "dry-run"
        first = self.put("a.txt", "work\n", "card work")
        sha = self.put("b.txt", "more\n", "more work")
        self.land()                                  # the check that recorded the base
        git(self.repo, "push", "-q", str(self.remote), f"{sha}:refs/heads/main")   # by hand
        self.cfg["lander"]["mode"] = "land"
        got = self.land(fetch=True)
        self.assertEqual((got["outcome"], got["pushed"]), ("already_on_main", False))
        state = db.guard_get(self.conn, landing.LANDED + self.tid)[0]
        self.assertEqual(state["commits"], [first, sha])
        self.assertEqual(self.status(), "done")
        self.assertEqual(self.remote_main(), sha)

    def test_without_a_recorded_base_it_asks_and_records_no_landing(self):
        sha = self.put("a.txt", "work\n", "card work")
        git(self.repo, "push", "-q", str(self.remote), f"{sha}:refs/heads/main")
        got = self.land(fetch=True)
        self.assertEqual(got["outcome"], "already_on_main")
        self.assertIsNone(db.guard_get(self.conn, landing.LANDED + self.tid)[0])
        self.assertEqual(self.status(), "review")
        lines = [r[0] for r in self.conn.execute("SELECT body FROM outbox")]
        self.assertTrue(any("already on origin/main" in line for line in lines), lines)


class PushFailed(Landing):
    def asks(self):
        return [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE body LIKE '%push to origin failed%'")]

    def runs(self):
        path = self.dir / "runs"
        return len(path.read_text().split()) if path.exists() else 0

    def test_auth_or_network_failures_back_off_ask_once_and_then_open_an_incident(self):
        self.cfg["contract_commands"] = [f"echo x >> {self.dir}/runs"]
        head = self.put("a.txt", "work\n", "card work")
        git(self.release, "remote", "set-url", "origin", str(self.dir / "nowhere.git"))
        self.assertEqual(self.land()["outcome"], "push_failed")
        self.assertEqual((self.runs(), len(self.asks())), (1, 1))
        self.assertEqual(self.land()["outcome"], "push_backoff")    # no suites re-run
        self.assertEqual(self.runs(), 1)
        db.guard_set(self.conn, landing.PUSHFAIL, {"count": 1, "until": 0})
        self.assertEqual(self.land()["outcome"], "push_failed")
        self.assertEqual((self.runs(), len(self.asks())), (2, 1))
        self.assertEqual(self.conn.execute("SELECT kind FROM incidents").fetchone()[0],
                         "silent_failure")
        git(self.release, "remote", "set-url", "origin", str(self.remote))
        db.guard_set(self.conn, landing.PUSHFAIL, {"count": 2, "until": 0})
        self.assertEqual(self.land()["outcome"], "landed")
        self.assertEqual(self.remote_main(), head)
        self.assertIsNone(db.guard_get(self.conn, landing.PUSHFAIL)[0])

    def test_a_hook_or_branch_protection_decline_is_a_failure_not_a_rejection(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")           # git itself prints "[remote rejected]"
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
        self.put("a.txt", "work\n", "card work")
        self.assertEqual(self.land()["outcome"], "push_failed")
        self.assertEqual(db.guard_get(self.conn, landing.PUSHFAIL)[0]["count"], 1)
        self.assertEqual(len(self.asks()), 1)
        self.assertEqual(self.land()["outcome"], "push_backoff")

    def test_a_rejection_is_not_a_failure_and_starts_no_back_off(self):
        self.put("a.txt", "work\n", "card work")
        self.rival()
        self.assertEqual(self.land()["outcome"], "push_rejected")
        self.assertIsNone(db.guard_get(self.conn, landing.PUSHFAIL)[0])
        self.assertEqual(self.asks(), [])


if __name__ == "__main__":
    unittest.main()
