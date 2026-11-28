# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Verify.test_pending_past_the_bound_asks_owner_and_does_not_revert
"""LAND-07: the GitHub CI conclusion for a pushed main as a second red source for `landing.verify`.
Test names carry the intent row ids of contracts/intent/land_reliability.jsonl. The fake `gh` in
fixtures/fakebin stands in for GitHub (never reached); git is real over a temp bare remote."""
import contextlib
import io
import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from capsule import ci_watch, intent, landing
from capsule.db import guard_get, guard_set
from capsule.tests.test_landing import NOW, Landing, git

FAKEBIN = Path(__file__).resolve().parent / "fixtures" / "fakebin"
URL = "https://github.com/owner/capsule-corp.git"
SHA = "a" * 40
GREEN = json.dumps([{"conclusion": "success", "status": "completed"}])
RED = json.dumps([{"conclusion": "failure", "status": "completed"}])
PENDING = json.dumps([{"conclusion": "", "status": "in_progress"}])


class Conclusion(unittest.TestCase):
    def ask(self, stdout="[]", rc=0, gh=None):
        env = {"FAKE_GH_STDOUT": stdout, "FAKE_GH_RC": str(rc), "PATH": f"{FAKEBIN}:/usr/bin"}
        with mock.patch.dict(os.environ, env):
            return ci_watch.conclusion(URL, SHA, **({"gh": gh} if gh else {}))

    def test_the_four_answers(self):
        self.assertEqual(self.ask(GREEN), "green")
        self.assertEqual(self.ask(RED), "red")
        self.assertEqual(self.ask(PENDING), "pending")
        self.assertEqual(self.ask("[]"), "pending")      # CI not registered for the push yet

    def test_red_beats_a_workflow_still_running(self):
        both = json.dumps([json.loads(RED)[0], json.loads(PENDING)[0]])
        self.assertEqual(self.ask(both), "red")

    def test_cancelled_or_skipped_alone_is_not_a_verdict(self):
        gone = json.dumps([{"conclusion": "cancelled", "status": "completed"}])
        self.assertEqual(self.ask(gone), "unknown")

    def test_gh_unavailable_is_infra_not_red(self):       # gh-unavailable-is-infra-not-red
        self.assertEqual(self.ask(rc=4), "unknown")                      # auth or network
        self.assertEqual(self.ask("not json"), "unknown")
        self.assertEqual(self.ask(gh="/nonexistent/gh"), "unknown")     # gh not installed

    def test_the_command_names_the_repo_branch_and_sha(self):
        argv = Path(os.environ.get("TMPDIR", "/tmp")) / f"gh-argv-{os.getpid()}"
        self.addCleanup(argv.unlink, missing_ok=True)
        with mock.patch.dict(os.environ, {"FAKE_GH_ARGV": str(argv)}):
            self.ask(GREEN)
        got = json.loads(argv.read_text().splitlines()[0])
        self.assertEqual(got[:2], ["run", "list"])
        for flag, value in (("--workflow", "verify.yml"), ("--branch", "main"), ("--commit", SHA),
                            ("-R", URL), ("--json", "conclusion,status")):
            self.assertEqual(got[got.index(flag) + 1], value)

    def test_a_run_waiting_for_approval_is_pending_not_red(self):
        held = json.dumps([{"conclusion": "action_required", "status": "completed"}])
        self.assertEqual(self.ask(held), "pending")


class Shell(unittest.TestCase):
    def run_main(self, url, stdout=RED):
        out = io.StringIO()
        env = {"FAKE_GH_STDOUT": stdout, "PATH": f"{FAKEBIN}:/usr/bin"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            rc = ci_watch.main([url, SHA])
        return rc, out.getvalue().strip()

    def test_the_shell_check_prints_the_conclusion(self):
        self.assertEqual(self.run_main(URL), (0, "red"))
        self.assertEqual(self.run_main(URL, GREEN), (0, "green"))


class Verify(Landing):
    def setUp(self):
        super().setUp()
        self.cfg["lander"]["ci_watch"] = True
        self.argv = self.dir / "gh-argv"
        self.counter = self.dir / "suite-runs"
        self.cfg["contract_commands"] = [f"echo x >> {self.counter}"]
        self.gh({"FAKE_GH_STDOUT": GREEN, "FAKE_GH_ARGV": str(self.argv)})
        self.put("a.txt", "work\n", "card work")
        self.sha = self.put("b.txt", "more\n", "more work")
        self.land()
        self.runs0 = self.suite_runs()

    def suite_runs(self):
        return len(self.counter.read_text().splitlines())

    def gh(self, env):
        patcher = mock.patch.dict(os.environ, {"PATH": f"{FAKEBIN}:{os.environ['PATH']}", **env})
        patcher.start()
        self.addCleanup(patcher.stop)

    def red_sha(self, parent=GREEN):
        """The landed sha is red; main's tip before the push (the card's base) answers `parent`."""
        base = git(self.release, "rev-parse", f"{self.sha}~2")
        self.gh({"FAKE_GH_STDOUT": RED, "FAKE_GH_BY_COMMIT": json.dumps({base: parent})})

    def verify(self, at=5):
        return landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release,
                              now=NOW + at)

    def incidents(self):
        return self.conn.execute("SELECT kind, task_id FROM incidents").fetchall()

    def lines(self):
        return [r[0] for r in self.conn.execute("SELECT body FROM outbox")]

    def reject_pushes(self):
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text('#!/bin/sh\necho "! [rejected] x" >&2\nexit 1\n')
        os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)

    def verified_detail(self):
        rows = self.conn.execute("SELECT payload FROM task_events WHERE kind='landing'")
        return [d["detail"] for d in map(json.loads, (r[0] for r in rows))
                if d["outcome"] == "verified"]

    def test_green_ci_closes_watch(self):                  # green-ci-closes-watch
        self.assertEqual(self.verify()["outcome"], "verified")
        self.assertEqual(self.verify()["outcome"], "nothing")
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.verified_detail(), ["ci: green"])

    def test_gh_is_asked_about_the_release_checkouts_origin(self):
        self.verify()
        got = json.loads(self.argv.read_text().splitlines()[0])
        self.assertEqual(got[got.index("-R") + 1], git(self.release, "remote", "get-url", "origin"))

    def test_the_default_config_never_calls_gh(self):      # off unless lander.ci_watch is set
        del self.cfg["lander"]["ci_watch"]
        self.gh({"FAKE_GH_STDOUT": RED})                   # a red answer would revert if asked
        self.assertEqual(self.verify()["outcome"], "verified")
        self.assertFalse(self.argv.exists())
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.verified_detail(), ["ci: off"])

    def test_red_main_ci_opens_incident_once(self):        # red-main-ci-opens-incident-once
        self.red_sha()
        self.reject_pushes()                  # the revert stays owed, so the next pass retries it
        self.assertEqual(self.verify()["outcome"], "revert_push_rejected")
        self.assertEqual(self.verify(10)["outcome"], "revert_push_rejected")
        (incident,) = self.incidents()
        self.assertEqual((incident["kind"], incident["task_id"]), ("escaped_bug", self.tid))
        self.assertEqual(self.remote_main(), self.sha)

    def test_red_main_ci_reverts_with_a_revert_commit_never_a_reset(self):
        self.red_sha()
        self.assertEqual(self.verify()["outcome"], "reverted")
        git(self.remote, "merge-base", "--is-ancestor", self.sha, "main")
        self.assertIn("Revert", git(self.remote, "log", "-1", "--format=%s", "main"))
        self.assertEqual(len(self.incidents()), 1)

    def test_ci_pending_waits_never_reverts(self):         # ci-pending-waits-never-reverts
        self.gh({"FAKE_GH_STDOUT": PENDING})
        self.assertEqual(self.verify()["outcome"], "ci_pending")
        self.assertEqual(self.verify(600)["outcome"], "ci_pending")
        self.assertEqual(self.remote_main(), self.sha)
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.suite_runs(), self.runs0 + 1)   # suites ran once
        self.gh({"FAKE_GH_STDOUT": GREEN})
        self.assertEqual(self.verify(700)["outcome"], "verified")

    def test_pending_past_the_bound_asks_owner_and_does_not_revert(self):
        self.cfg["lander"]["ci_wait_s"] = 100
        self.gh({"FAKE_GH_STDOUT": PENDING})
        self.assertEqual(self.verify()["outcome"], "ci_pending")
        self.assertEqual(self.verify(200)["outcome"], "ci_unverified")
        self.assertEqual(self.remote_main(), self.sha)
        self.assertEqual(self.incidents(), [])
        self.assertEqual(len([x for x in self.lines() if "CI" in x]), 1)
        self.assertEqual(self.verify(300)["outcome"], "nothing")   # one question, watch closed

    def test_gh_unavailable_is_infra_not_red_in_verify(self):   # gh-unavailable-is-infra-not-red
        self.cfg["lander"]["ci_wait_s"] = 100
        self.gh({"FAKE_GH_RC": "1", "FAKE_GH_STDERR": "gh: auth failed"})
        self.assertEqual(self.verify()["outcome"], "ci_pending")
        self.assertEqual(self.verify(200)["outcome"], "ci_unverified")
        self.assertEqual(self.remote_main(), self.sha)
        self.assertEqual(self.incidents(), [])
        self.assertTrue([x for x in self.lines() if "CI" in x])

    def replay(self, sha_name):
        """The stamped intent row, one sha of it: its gh runs through the fake gh, its passes
        through verify, then its expectations read from the board and the remote."""
        (row,) = [r for r in intent.load("land_reliability")
                  if r["id"] == "red-main-ci-opens-incident-once"]
        case = row["given"]["document"]["shas"][sha_name]
        want = row["expect"][sha_name]
        self.cfg["lander"]["ci_wait_s"] = row["given"]["document"]["ci_wait_s"]
        self.gh({"FAKE_GH_STDOUT": json.dumps(case["gh_runs"])})
        self.assertEqual(ci_watch.conclusion(URL, self.sha), want["state"])
        if want["state"] == "red":
            self.red_sha()              # the row is the card's escaped bug: its base was green
        if want["state"] == "red":
            self.reject_pushes()        # the revert stays owed, so each pass retries it
        if case.get("waited_s"):
            state, _ = guard_get(self.conn, landing.LANDED + self.tid)
            guard_set(self.conn, landing.LANDED + self.tid,
                      {**state, "ci_since": NOW + 5 - case["waited_s"]}, now=NOW)
        outcomes = [self.verify(5 + 5 * i)["outcome"] for i in range(case["verify_passes"])]
        state, _ = guard_get(self.conn, landing.LANDED + self.tid)
        self.assertEqual(len(self.incidents()), want["incidents_opened"])
        self.assertEqual(state["state"] == "revert_owed", want["revert_owed"])
        return outcomes, state

    def test_red_main_ci_opens_incident_once_replays_the_intent_row_red(self):
        outcomes, _ = self.replay("abc1234")
        self.assertEqual(outcomes, ["revert_push_rejected"] * 3)

    def test_green_ci_closes_watch_replays_the_intent_row_green(self):
        outcomes, state = self.replay("def5678")
        self.assertEqual((outcomes, state["state"]), (["verified"], "verified"))   # watch closed

    def test_ci_pending_waits_never_reverts_replays_the_intent_row_pending(self):
        outcomes, state = self.replay("0aa0001")
        self.assertEqual((outcomes, state["state"]), (["ci_pending"], "pushed"))
        self.assertEqual(self.remote_main(), self.sha)

    def test_red_sha_on_a_red_parent_is_pre_existing_red(self):   # CI watch: not the card's bug
        self.red_sha(parent=RED)
        self.assertEqual(self.verify()["outcome"], "verified")
        state, _ = guard_get(self.conn, landing.LANDED + self.tid)
        self.assertEqual(state["state"], "verified")        # never revert_owed
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.remote_main(), self.sha)      # no revert pushed
        self.assertEqual(self.verified_detail(), ["ci: pre-existing red"])
        self.assertEqual(self.verify(10)["outcome"], "nothing")

    def test_red_sha_on_a_green_parent_is_the_cards_escaped_bug(self):
        self.red_sha(parent=GREEN)
        self.assertEqual(self.verify()["outcome"], "reverted")
        self.assertEqual([tuple(i) for i in self.incidents()], [("escaped_bug", self.tid)])

    def test_red_sha_waits_while_the_parent_is_pending_or_unreadable(self):
        self.cfg["lander"]["ci_wait_s"] = 100
        self.red_sha(parent=PENDING)
        self.assertEqual(self.verify()["outcome"], "ci_pending")
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.remote_main(), self.sha)
        self.red_sha(parent="not json")                     # unreadable
        self.assertEqual(self.verify(50)["outcome"], "ci_pending")
        self.assertEqual(self.verify(200)["outcome"], "ci_unverified")   # bound passed: asks once
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.remote_main(), self.sha)

    def test_a_local_red_still_wins_without_asking_github(self):
        self.gh({"FAKE_GH_STDOUT": GREEN})
        self.cfg["contract_commands"] = ["exit 1"]
        self.assertEqual(self.verify()["outcome"], "reverted")


if __name__ == "__main__":
    unittest.main()
