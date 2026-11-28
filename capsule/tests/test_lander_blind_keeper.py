"""R35, S22, S23, S27, S31: `python3 -m capsule.lander --blind-only ...`, the keeper's gate for a
card it lands, and the after-push verify of the landing. The gate exits 0 only when a `blind` row
says pass or the card truly has no blind tester, and says which; every refusal exits 1 with a plain
reason. Fixtures in `lander_world`. Oracle 1 is the exit code and the line printed; oracle 2 is the
refs, the runner's `started` list and the board."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from capsule import db, incidents, lander, landing, paths, workspace
from capsule.db import event, guard_get, guard_set
from capsule.runners import docker, sandbox
from capsule.tests import test_landing
from capsule.tests.lander_world import BOARD, PASSING, TESTS, Landing, Strong
from capsule.tests.test_blind_run import record
from capsule.tests.test_lander import BRANCH, git
from capsule.tests.test_mount_audit import Fake, listing

AUDIT = f"{docker.BLIND_CHECK}: /somewhere [run capsule-abcdef123456]"
APPROVED = {"implementer": "pass", "reviewer": "pass", "validator": "pass", "guide": "pass",
            "rounds": 1, "overrides": []}


class KeeperRoute(Landing):
    """`--blind-only`: the keeper's gate for a card it lands."""
    source = PASSING

    def argv(self, *, task=True, commit=None, branch=BRANCH, blind_only=True):
        tip = git(self.repo, "rev-parse", BRANCH)
        return ["--repo", str(self.repo), "--branch", branch, "--commit", commit or tip,
                "--board", BOARD, "--home", str(self.home),
                *(["--task", self.card] if task else []), *(["--blind-only"] if blind_only else [])]

    def go(self, strong, argv=None, config=None):
        """(exit code, the line it said) of one run of the CLI."""
        cfg_path = paths.config_path(self.home, BOARD)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(config or self.cfg))
        err, self.out = io.StringIO(), io.StringIO()
        with mock.patch.object(sandbox, "runner_for", return_value=strong), \
                contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(err):
            return lander.main(argv or self.argv()), err.getvalue()

    def rows(self):
        """{name: row} of the verdict the last run printed."""
        return {r["name"]: r for r in json.loads(self.out.getvalue())["checks"]}

    def test_a_green_card_exits_zero_says_which_pass_and_pushes_nothing(self):
        before = self.refs()
        code, said = self.go(Strong())
        self.assertEqual(code, 0)
        self.assertIn("the blind step passed", said)
        self.assertEqual(self.refs(), before)

    def test_steps_that_did_not_run_read_skipped_never_pass(self):
        cfg = {**self.cfg, "contract_commands": ["false"]}
        self.assertEqual(self.go(Strong(), config=cfg)[0], 0)
        rows = self.rows()
        for name in ("stale_marker", "gates", "suites", "deploy"):
            self.assertEqual((rows[name]["status"], rows[name]["detail"]),
                             ("skipped", "not run: blind-only route"), name)
        for name in ("fetch", "sync", "protected", "blind"):
            self.assertEqual(rows[name]["status"], "pass", name)

    def test_the_full_route_still_runs_every_step(self):
        self.go(Strong(), self.argv(blind_only=False))
        self.assertNotIn("skipped", {r["status"] for r in self.rows().values()})

    def test_main_moving_a_file_the_test_reads_fails_the_merge_commit_with_exit_two(self):
        """The keeper's `Main moving` step merges origin/main into the batch: the commit it pushes
        is the merge, so g11 must run on it. Before the merge the card passes; after it, no."""
        self.commit_tests(PASSING.replace('run("echo no").exit, 0', 'run("test -e b.txt").exit, 1'))
        record(self.conn, self.card, self.blind)
        self.assertEqual(self.go(Strong())[0], 0)                  # the batch tip alone is green
        self.behind("b.txt")                                       # main adds a file the test reads
        git(self.repo, "merge", "-q", "--no-edit", "origin/main")
        merged = git(self.repo, "rev-parse", "HEAD")
        code, said = self.go(Strong(), self.argv(commit=merged))
        self.assertEqual(code, 2)
        self.assertIn("not landable: blind", said)
        self.assertIn("blind tests failed", said)

    def test_a_commit_given_as_a_short_prefix_of_the_tip_is_the_tip(self):
        tip = git(self.repo, "rev-parse", BRANCH)
        self.assertEqual(self.go(Strong(), self.argv(commit=tip[:8]))[0], 0)

    def test_a_failing_blind_test_exits_two_and_pushes_nothing(self):
        self.commit_tests(TESTS)
        record(self.conn, self.card, self.blind)
        before = self.refs()
        self.assertEqual(self.go(Strong())[0], 2)
        self.assertEqual(self.refs(), before)

    def test_a_run_that_cannot_be_trusted_exits_two_and_a_docker_that_is_down_three(self):
        self.assertEqual(self.go(Strong(tamper=lambda t: t.replace("Ran 2", "Ran 1")))[0], 2)
        self.assertEqual(self.go(Strong(up=False))[0], 3)

    def test_it_runs_no_suites_and_a_failing_suite_command_does_not_matter(self):
        cfg = {**self.cfg, "contract_commands": ["false"]}
        self.assertEqual(self.go(Strong(), config=cfg)[0], 0)

    def test_a_card_that_gets_no_blind_tester_exits_zero_and_says_so(self):
        self.conn.execute("UPDATE tasks SET body='no acceptance here', blind_commit=NULL"
                          " WHERE id=?", (self.card,))
        strong = Strong()
        code, said = self.go(strong)
        self.assertEqual((code, strong.started), (0, []))
        self.assertIn("gets no blind tester", said)

    def test_a_card_that_would_get_a_tester_but_has_no_tests_exits_two(self):
        self.conn.execute("UPDATE tasks SET blind_commit=NULL WHERE id=?", (self.card,))
        self.assertEqual(self.go(Strong())[0], 2)

    def test_a_board_that_is_not_in_gate_mode_is_refused_and_nothing_runs(self):
        strong = Strong()
        cfg = {**self.cfg, "redesign": {"blind_tests": "report"}}
        code, said = self.go(strong, config=cfg)
        self.assertEqual((code, strong.started), (1, []))
        self.assertIn("gate mode", said)

    def test_a_board_with_no_task_is_refused_in_both_modes(self):
        for blind_only in (True, False):
            strong = Strong()
            code, said = self.go(strong, self.argv(task=False, blind_only=blind_only))
            self.assertEqual((code, strong.started), (1, []), blind_only)
            self.assertIn("--task is required", said)

    def test_an_unknown_card_is_refused_and_nothing_runs(self):
        strong = Strong()
        code, said = self.go(strong, self.argv()[:-3] + ["--task", "t_00000000", "--blind-only"])
        self.assertEqual((code, strong.started), (1, []))
        self.assertIn("t_00000000 is not a card", said)

    def test_a_commit_that_is_not_the_branch_tip_is_refused_and_nothing_runs(self):
        old = git(self.repo, "rev-parse", BRANCH)
        self.put("later.txt", "later\n", "later work")
        strong = Strong()
        code, said = self.go(strong, self.argv(commit=old))
        self.assertEqual((code, strong.started), (1, []))
        self.assertIn("not the tip", said)

    def test_a_branch_behind_main_is_refused_never_replayed(self):
        self.behind("b.txt")
        strong = Strong()
        code, said = self.go(strong)
        self.assertEqual((code, strong.started), (1, []))
        self.assertIn("behind", said)

    def test_a_branch_whose_tip_is_already_on_main_is_refused_nothing_ran(self):
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--ff-only", BRANCH)
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", BRANCH)
        strong = Strong()
        code, said = self.go(strong)
        self.assertEqual((code, strong.started), (1, []))
        self.assertIn("already on origin/main", said)


class Scope(unittest.TestCase):
    def test_the_landers_runner_carries_the_boards_blind_repo_and_spool_root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = {"board": BOARD, "blind_tests": {"repo": tmp, "adapter": "unittest"}}
        runner = sandbox.runner_for(cfg)
        self.assertEqual(runner.blind_repo, os.path.realpath(tmp))
        self.assertEqual(runner.spool_root, paths.board_dir(BOARD) / "spool")
        self.assertEqual(runner.blind_work, paths.board_dir(BOARD) / "blind-work")
        self.assertIsNone(sandbox.runner_for({"board": BOARD}).blind_repo)

    def audit(self, runner, *sources):
        fake = Fake(listing(*map(str, sources)))
        fake.exists, runner._run = True, fake
        return runner._audit("capsule-x")

    def test_the_landers_audit_refuses_the_testers_clone_the_ticks_runner_does_not_look(self):
        work = paths.board_dir(BOARD) / "blind-work" / ("a" * 32)
        lander_runner = sandbox.runner_for({"board": BOARD})
        for source in (work, work / "tests", work.parent):
            with self.assertRaises(RuntimeError, msg=str(source)) as caught:
                self.audit(lander_runner, "/deps", source)
            self.assertIn(docker.BLIND_CHECK, str(caught.exception))
        self.assertEqual(self.audit(lander_runner, "/deps", paths.board_dir(BOARD) / "other"),
                         (0, ""))
        self.assertEqual(self.audit(docker.StrongRunner(board=BOARD), work), (0, ""))   # the tick's


class Trusted(unittest.TestCase):
    """S30: the keeper gate runs the trusted checkout's lander, and what that lander judges with is
    protected, so a batch cannot change the code that judges it."""
    BRIEF = paths.REPO / "maintenance" / "templates" / "keeper_brief.md"

    def test_the_brief_runs_the_lander_from_the_trusted_checkout_not_the_batch_worktree(self):
        text = self.BRIEF.read_text()
        self.assertIn("cd {{repo}} && python3 -m capsule.lander --blind-only --repo {{worktree}}",
                      text)
        self.assertNotIn("cd {{worktree}} && python3 -m capsule.lander", text)

    def test_main_moving_reruns_g11_on_the_merge_commit_never_the_old_head(self):
        text = self.BRIEF.read_text()
        moving = next(x for x in text.splitlines() if x.startswith("Main moving"))
        self.assertIn(" g11 ", moving)
        self.assertIn("`git -C {{worktree}} rev-parse HEAD`", moving)
        self.assertIn("never {{head8}}", moving)

    def test_what_the_lander_judges_a_blind_run_with_is_protected(self):
        judges = ["capsule/lander.py", "capsule/lander_blind.py", "capsule/blind/run.py",
                  "capsule/blind/gate.py", "capsule/blind/preflight.py",
                  "capsule/incident_blind.py", "capsule/runners/docker.py",
                  "capsule/runners/sandbox.py"]
        self.assertEqual(workspace.touches_protected(judges), judges)


class VerifyAudit(test_landing.Landing):
    """S31: an audit hit on the after-push verify is a hold, not a crash that respawns for ever."""

    def setUp(self):
        super().setUp()
        self.cfg["repo_url"] = str(self.repo)
        self.sha = self.put("a.txt", "work\n", "card work")
        guard_set(self.conn, landing.LANDED + self.tid,
                  {"sha": self.sha, "commits": [self.sha], "state": "pushed"}, now=test_landing.NOW)

    def state(self):
        return guard_get(self.conn, landing.LANDED + self.tid)[0]["state"]

    def verify(self):
        with mock.patch.object(landing, "_suites_red", side_effect=RuntimeError(AUDIT)):
            return landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release,
                                  now=test_landing.NOW + 1)

    def test_an_audit_hit_leaves_pushed_opens_the_incident_and_tells_a_person(self):
        got = self.verify()
        self.assertEqual(got["outcome"], "verify_refused")
        self.assertEqual(self.state(), "verify_refused")
        self.assertEqual([i["role"] for i in incidents.listing(self.conn)],
                         [f"{docker.BLIND_CHECK}:{self.tid}"])
        sent = json.dumps([tuple(r) for r in self.conn.execute("SELECT * FROM outbox")])
        self.assertIn("NOT verified", sent)

    def test_no_pass_respawns_the_verify_and_another_card_still_lands(self):
        self.verify()
        spawned = []
        young = self.put("b.txt", "young\n", "young work")
        card = db.new_task(self.conn, title="y", assignee="dev", branch=BRANCH, status="done")
        event(self.conn, card, "completed", {"commit": young, "gates": APPROVED},
              now=test_landing.NOW + 2)
        self.conn.commit()

        def spy(conn, cfg, home, now, release, action, task_id, sha=None):
            spawned.append((action, task_id))
            return "running"

        with mock.patch.object(landing, "_spawn", spy):
            landing.step(self.conn, self.cfg, self.home, test_landing.NOW + 3,
                         release=self.release)
        self.assertEqual(spawned, [("land", card)])

    def test_a_persons_verify_re_arms_a_refused_verify_and_green_suites_close_the_watch(self):
        self.verify()
        self.assertEqual(self.state(), "verify_refused")
        with mock.patch.object(landing, "_suites_red", return_value=("pass", "")):
            got = landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release,
                                 now=test_landing.NOW + 2)
        self.assertEqual((got["outcome"], self.state()), ("verified", "verified"))

    def test_a_re_armed_verify_the_audit_refuses_again_is_refused_again_not_nothing(self):
        self.verify()
        got = self.verify()
        self.assertEqual((got["outcome"], self.state()), ("verify_refused", "verify_refused"))

    def test_the_ask_names_what_works_to_get_the_commit_verified(self):
        self.verify()
        sent = json.dumps([tuple(r) for r in self.conn.execute("SELECT * FROM outbox")])
        self.assertIn(f"python3 -m capsule.landing verify --task {self.tid}", sent)
        for option in ("--db", "--repo", "--release", "--config"):
            self.assertIn(option, sent)
        self.assertIn("NOT verified", sent)
        self.assertIn("A RED result pushes a revert to main by itself", sent)

    def test_an_error_that_is_no_audit_hit_is_still_raised(self):
        with mock.patch.object(landing, "_suites_red", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release,
                               now=test_landing.NOW + 1)
        self.assertEqual(self.state(), "pushed")


if __name__ == "__main__":
    unittest.main()
