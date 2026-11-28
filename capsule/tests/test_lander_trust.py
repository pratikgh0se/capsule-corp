"""The lander's gate scripts and deploy check run from the trusted checkout (paths.REPO), never
from the candidate clone. Real git over the same fixture as test_lander; the trusted checkout is
a temp dir whose scripts record where and how they ran, the candidate's own scripts record
nothing but their marker. Also replays contracts/intent/lander_trust.jsonl (Z1)."""
import contextlib
import json
import os
import unittest
from unittest import mock

from capsule import db, intent, lander
from capsule.runners import sandbox
from capsule.tests.test_lander import World, git

# The gate of the row candidate-gate-script-never-runs. Since row 632 b it runs the candidate's
# tests, so it runs in the sandbox from /trusted, not on the host (lander.HOST_GATES).
BOXED_GATE = "python3 tests/mutation_check.py --base {base} --head {head}"

# Z1, contracts/intent/lander_trust.jsonl: what each example's case must observe. The cases run on
# these built-in expectations alone, so they hold before (and without) the rows file.
EXPECT = {
    "candidate-gate-script-never-runs": {
        "gates": "pass", "trusted_gate_ran_with": "--repo <clone>", "candidate_marker": "absent"},
    "missing-trusted-gate-fails-closed": {
        "gates": "fail", "detail": "gate script missing from trusted checkout: tests/new_gate.py",
        "candidate_marker": "absent"},
    "candidate-deploy-script-never-runs": {
        "deploy": "pass", "candidate_marker": "absent", "trusted_deploy_check_ran": True},
}


class TrustedChecks(World):
    """A worker's edit of its own gate or deploy script runs nowhere."""

    def setUp(self):
        super().setUp()
        self.trusted = self.dir / "trusted"
        (self.trusted / "tests").mkdir(parents=True)
        self.seen = self.dir / "seen"
        self.seen.mkdir()
        # size_check only reads text, so it runs on the host; mutation_check runs the tests, so it
        # runs in the sandbox from /trusted (the in-process host stand-in maps /trusted here)
        self.script(self.trusted / "tests" / "size_check.py", "trusted-gate", "python")
        self.script(self.trusted / "tests" / "mutation_check.py", "trusted-box-gate", "python")
        self.script(self.trusted / "deploy.sh", "trusted-deploy", "sh")
        patch = mock.patch.object(lander.paths, "REPO", self.trusted)
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg["lander"]["gates"] = [
            "python3 tests/size_check.py --base {base} --head {head}"]
        self.cfg["lander"]["deploy_check"] = "./deploy.sh --check"

    def script(self, path, who, kind):
        """A script that records its cwd and arguments in seen/<who> (a python one also its own
        path in seen/<who>.script)."""
        out = self.seen / who
        body = (f"import os, sys\nopen({str(out)!r}, 'w').write(os.getcwd() + ' ' + "
                f"' '.join(sys.argv[1:]))\nopen({str(out) + '.script'!r}, 'w').write("
                "os.path.realpath(__file__))\n" if kind == "python" else
                f'#!/bin/sh\necho "$PWD $*" > {out}\n')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)

    def candidate(self):
        """The worker's commit: its own size_check.py, mutation_check.py and deploy.sh."""
        self.script(self.repo / "tests" / "size_check.py", "candidate-gate", "python")
        self.script(self.repo / "tests" / "mutation_check.py", "candidate-box-gate", "python")
        self.script(self.repo / "deploy.sh", "candidate-deploy", "sh")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "worker edits its gate and deploy script")

    def own_gate(self, name="own_gate"):
        self.script(self.repo / "tests" / f"{name}.py", "own-gate", "python")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "card work")

    def ran(self, who):
        path = self.seen / who
        return path.read_text() if path.exists() else None

    def marker(self, who):
        return "absent" if self.ran(who) is None else "present"

    def test_the_candidates_own_gate_script_never_runs(self):
        self.candidate()
        result = self.check()
        self.assertEqual(self.status(result)["gates"], "pass", self.detail(result, "gates"))
        self.assertIsNone(self.ran("candidate-gate"))
        cwd, _, args = self.ran("trusted-gate").partition(" ")
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(self.trusted))
        self.assertIn("--base", args)
        self.assertTrue(args.split()[-1].endswith("/c"), args)   # --repo <the throwaway clone>

    def test_the_candidates_own_deploy_script_never_runs(self):
        self.candidate()
        result = self.check()
        self.assertEqual(self.status(result)["deploy"], "pass", self.detail(result, "deploy"))
        self.assertIsNone(self.ran("candidate-deploy"))
        self.assertEqual(os.path.realpath(self.ran("trusted-deploy").split()[0]),
                         os.path.realpath(self.trusted))

    def test_a_gate_the_trusted_checkout_lacks_fails_closed_and_never_runs(self):
        self.own_gate()
        self.cfg["lander"]["gates"] = ["python3 tests/own_gate.py", "echo other-gate"]
        result = self.check()
        self.assertEqual(self.status(result)["gates"], "fail")
        self.assertIn("gate script missing from trusted checkout: tests/own_gate.py",
                      self.detail(result, "gates"))
        self.assertIsNone(self.ran("own-gate"))
        self.assertEqual(result["verdict"], "red")

    def test_a_gate_of_another_shape_still_runs_as_written_in_the_clone(self):
        self.own_gate()
        self.cfg["lander"]["gates"] = ["python3 -u tests/own_gate.py", "echo other-gate"]
        result = self.check()
        self.assertEqual(self.status(result)["gates"], "pass", self.detail(result, "gates"))
        self.assertNotEqual(os.path.realpath(self.ran("own-gate").split()[0]),
                            os.path.realpath(self.trusted))
        self.assertIn("other-gate", self.detail(result, "gates"))

    def boxed_run(self):
        """check() with the sandbox's commands recorded: (result, the commands handed to the box).
        The recorder wraps the in-process host stand-in, which maps /trusted and /workspace."""
        commands, real = [], sandbox.session

        @contextlib.contextmanager
        def session(*args, **kwargs):
            with real(*args, **kwargs) as run:
                if run is None:
                    yield None
                    return

                def spy(cmd, timeout):
                    commands.append(cmd)
                    return run(cmd, timeout)
                yield spy

        with mock.patch.object(sandbox, "session", session):
            return self.check(), commands

    def test_a_boxed_gate_runs_from_trusted_in_the_sandbox_never_the_candidates_script(self):
        """The row's gate (mutation_check) runs the candidate's tests, so it is boxed: the only
        command is the trusted script by its /trusted path on the clone at /workspace, and the
        candidate's own tests/mutation_check.py (and size_check.py) wrote no marker."""
        self.cfg["lander"]["gates"] = [BOXED_GATE]
        self.candidate()
        result, commands = self.boxed_run()
        self.assertEqual(self.status(result)["gates"], "pass", self.detail(result, "gates"))
        gate_cmds = [c for c in commands if "mutation_check" in c]
        self.assertEqual(len(gate_cmds), 1, commands)
        self.assertTrue(gate_cmds[0].startswith("python3 /trusted/tests/mutation_check.py --base "),
                        gate_cmds[0])
        self.assertTrue(gate_cmds[0].endswith(" --repo /workspace"), gate_cmds[0])
        self.assertFalse([c for c in commands if "/workspace/tests" in c or "./tests" in c])
        self.assertIsNone(self.ran("candidate-box-gate"))
        self.assertIsNone(self.ran("candidate-gate"))
        self.assertEqual(self.ran("trusted-box-gate.script"),
                         os.path.realpath(self.trusted / "tests" / "mutation_check.py"))
        self.assertIsNone(self.ran("trusted-gate"))      # the host gate did not stand in for it

    def observe_candidate_gate_script_never_runs(self):
        self.cfg["lander"]["gates"] = [BOXED_GATE]
        self.candidate()
        result, commands = self.boxed_run()
        script, (_, _, args) = self.ran("trusted-box-gate.script"), (
            (self.ran("trusted-box-gate") or "").partition(" "))
        argv = args.split()
        boxed_trusted = (script == os.path.realpath(self.trusted / "tests" / "mutation_check.py")
                         and any(c.startswith("python3 /trusted/tests/mutation_check.py ")
                                 for c in commands))
        on_clone = len(argv) > 1 and argv[-2] == "--repo" and argv[-1].endswith("/c")
        ran_marks = (self.marker("candidate-box-gate"), self.marker("candidate-gate"))
        return {"gates": self.status(result)["gates"],
                "trusted_gate_ran_with": "--repo <clone>" if boxed_trusted and on_clone else args,
                "candidate_marker": "absent" if set(ran_marks) == {"absent"} else "present"}

    def observe_missing_trusted_gate_fails_closed(self):
        self.own_gate("new_gate")
        self.cfg["lander"]["gates"] = ["python3 tests/new_gate.py"]
        result = self.check()
        return {"gates": self.status(result)["gates"],
                "detail": self.detail(result, "gates").split("\n", 1)[1],
                "candidate_marker": self.marker("own-gate")}

    def observe_candidate_deploy_script_never_runs(self):
        self.candidate()
        result = self.check()
        cwd = (self.ran("trusted-deploy") or "").partition(" ")[0]
        return {"deploy": self.status(result)["deploy"],
                "candidate_marker": self.marker("candidate-deploy"),
                "trusted_deploy_check_ran":
                    bool(cwd) and os.path.realpath(cwd) == os.path.realpath(self.trusted)}

    def replay(self, case):
        """Observe one case on a fresh world (setUp again: new temp dir, new trusted checkout)."""
        self.setUp()
        return {"candidate-gate-script-never-runs": self.observe_candidate_gate_script_never_runs,
                "missing-trusted-gate-fails-closed": self.observe_missing_trusted_gate_fails_closed,
                "candidate-deploy-script-never-runs":
                    self.observe_candidate_deploy_script_never_runs}[case]()

    def test_each_case_observes_its_built_in_expectation(self):
        for case, want in EXPECT.items():
            self.assertEqual(self.replay(case), want, case)

    def test_the_shipped_rows(self):
        rows = intent.load("lander_trust")
        if not rows:
            self.skipTest("contracts/intent/lander_trust.jsonl is not shipped yet")
        self.assertEqual({r["id"] for r in rows}, set(EXPECT))
        for ex in rows:
            seen = self.replay(ex["id"])
            doc = ex["given"]["document"]    # what the row says it set up is what the case set up
            self.assertIn(doc.get("gate", self.cfg["lander"]["gates"][0]),
                          self.cfg["lander"]["gates"], ex["id"])
            self.assertEqual(doc.get("deploy_check", self.cfg["lander"]["deploy_check"]),
                             self.cfg["lander"]["deploy_check"], ex["id"])
            for key, value in ex["expect"].items():
                self.assertEqual(seen[key], value, f"{ex['id']}: {key}")

    def env_gate(self, refuse, code=75, named=("capsule/foo.py",), noise=""):
        """A trusted mutation_check that refuses (`environment error`, exit `code`, `named` the
        paths it says are dirty) on its first `refuse` runs and passes after, printing `noise`
        every time; the run count is in seen/runs."""
        runs = self.seen / "runs"
        (self.trusted / "tests" / "mutation_check.py").write_text(
            f"import pathlib, sys\np = pathlib.Path({str(runs)!r})\n"
            "n = int(p.read_text()) + 1 if p.exists() else 1\np.write_text(str(n))\n"
            "print('environment error: uncommitted changes to gate-critical paths:')\n"
            f"print({noise!r})\n" + "".join(f"print({('  ' + path)!r})\n" for path in named) +
            "print('Commit them first, or pass --allow-dirty for a local experiment only.')\n"
            f"sys.exit({code} if n <= {refuse} else 0)\n")
        self.cfg["lander"]["gates"] = [BOXED_GATE]
        return lambda: int(runs.read_text())

    def recorded(self, result):
        """(status, block_kind, failure_count) of a done card after `lander.record(result)`, and
        the connection."""
        board = self.dir / "board.db"
        db.create_board(board)
        conn = db.connect(board)
        self.addCleanup(conn.close)
        tid = db.new_task(conn, title="t", assignee="dev", branch="card", status="done")
        lander.record(conn, tid, result, limit=3, now=5)
        row = conn.execute("SELECT status, block_kind, failure_count FROM tasks WHERE id=?",
                           (tid,)).fetchone()
        return tuple(row), conn

    def test_an_environment_refusal_is_tried_once_more_and_the_landing_goes_on(self):
        runs = self.env_gate(refuse=1)
        result = self.check()
        self.assertEqual((self.status(result)["gates"], result["verdict"]), ("pass", "checked"))
        self.assertEqual(runs(), 2)

    def test_a_pass_on_the_second_try_leaves_a_flaky_record_naming_the_first_refusals_paths(self):
        self.env_gate(refuse=1)
        result = self.check()
        row, conn = self.recorded(result)
        self.assertEqual(row, ("done", None, 0))
        (note,) = [json.loads(p)["detail"] for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing_flaky'")]
        self.assertIn("environment error, then green on retry", note)
        self.assertIn("capsule/foo.py", note)
        self.assertNotIn("--allow-dirty", note)

    def test_two_environment_refusals_wait_and_are_never_a_counted_failure(self):
        runs = self.env_gate(refuse=2)
        result = self.check()
        self.assertEqual((self.status(result)["gates"], result["verdict"]), ("wait", "wait"))
        self.assertEqual((result["hold"], runs()), ("lander_wait_env", 2))   # one retry, no loop
        detail = self.detail(result, "gates")
        self.assertIn(lander.ENV_WAIT, detail)
        self.assertIn("capsule/foo.py", detail)
        self.assertNotIn("--allow-dirty", detail)
        self.assertEqual(self.recorded(result)[0], ("blocked", "transient", 0))

    def test_a_named_environment_error_without_paths_is_the_wait_detail(self):
        self.env_gate(refuse=2, named=())
        detail = self.detail(self.check(), "gates")
        self.assertIn(f"{lander.ENV_WAIT}: environment error: uncommitted changes to "
                      "gate-critical paths", detail)
        self.assertNotIn("stale share", detail)

    def test_a_very_long_environment_error_line_is_cut_and_the_wait_stays_an_env_wait(self):
        self.env_gate(refuse=2, named=())
        gate = self.trusted / "tests" / "mutation_check.py"
        gate.write_text("import sys\nprint('environment error: ' + 'x' * 3000)\nsys.exit(75)\n")
        result = self.check()
        self.assertEqual(result["hold"], "lander_wait_env")
        self.assertIn(lander.ENV_WAIT, self.detail(result, "gates"))

    def test_the_environment_wait_stays_visible_when_the_earlier_output_is_long(self):
        self.env_gate(refuse=2)
        self.cfg["lander"]["gates"] = ["python3 -c \"print('x' * 3000)\"", BOXED_GATE]
        detail = self.detail(self.check(), "gates")
        self.assertLessEqual(len(detail), lander.DETAIL)
        self.assertIn(lander.ENV_WAIT, detail)

    def crlf(self, where):
        """The commit that makes a fresh checkout dirty: a CRLF blob under a later `text`
        attribute (`git add -A` would renormalize the blob, so the attribute is added alone)."""
        self.put(f"{where}/crlf.txt", "a\r\nb\r\n", "a CRLF file")
        (self.repo / ".gitattributes").write_text(f"{where}/*.txt text\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-qm", "then a text attribute")

    def test_two_refusals_when_the_host_clone_is_dirty_are_the_commits_fault_and_red(self):
        runs = self.env_gate(refuse=9, named=("capsule/crlf.txt",))
        self.crlf("capsule")
        result = self.check()
        self.assertEqual((self.status(result)["gates"], result["verdict"]), ("fail", "red"))
        self.assertEqual(runs(), 2)
        self.assertIn("capsule/crlf.txt", self.detail(result, "gates"))
        self.assertEqual(self.recorded(result)[0], ("ready", None, 1))

    def test_quoted_file_names_in_the_refusal_still_match_the_dirty_files(self):
        """The gate prints paths as git quotes them (a space, a quote, a non-ASCII letter), so
        the oracle compares git's own text: a pathspec of that text matches nothing."""
        for name in ("a b.py", "caf\u00e9.py", 'q"x.py'):
            self.put(f"capsule/{name}", "a\r\nb\r\n", f"CRLF {name}")
        (self.repo / ".gitattributes").write_text("capsule/* text\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-qm", "then a text attribute")
        quoted = ('"capsule/a b.py"', '"capsule/caf\\303\\251.py"', '"capsule/q\\"x.py"')
        for named in [(one,) for one in quoted] + [quoted]:    # each alone, then all three
            self.env_gate(refuse=9, named=named)
            result = self.check()
            self.assertEqual((self.status(result)["gates"], result["verdict"]), ("fail", "red"))
            for name in named:   # git's own status lines, not only the gate's echo of them
                self.assertIn(f"M {name}", self.detail(result, "gates"))

    def test_a_users_quotepath_false_does_not_turn_a_dirty_non_ascii_file_into_a_wait(self):
        """A global `core.quotepath=false` makes git print raw UTF-8 names, which never match the
        gate's quoted ones: the host status pins quotepath=true, so the dirty file reads red."""
        self.put("capsule/caf\u00e9.py", "a\r\nb\r\n", "CRLF non-ASCII")
        (self.repo / ".gitattributes").write_text("capsule/* text\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-qm", "then a text attribute")
        self.env_gate(refuse=9, named=('"capsule/caf\\303\\251.py"',))
        user = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.quotepath",
                "GIT_CONFIG_VALUE_0": "false"}
        with mock.patch.dict(os.environ, user):
            result = self.check()
        self.assertEqual((self.status(result)["gates"], result["verdict"]), ("fail", "red"))

    def test_a_dirty_file_the_refusal_did_not_name_is_not_blamed(self):
        runs = self.env_gate(refuse=9, named=("capsule/foo.py",))
        self.crlf("docs")
        result = self.check()
        self.assertEqual((self.status(result)["gates"], result["verdict"]), ("wait", "wait"))
        self.assertEqual(runs(), 2)
        self.assertEqual(self.recorded(result)[0], ("blocked", "transient", 0))

    def test_the_dirty_verdict_does_not_depend_on_the_checkouts_stat_cache(self):
        """On a real-sized tree a plain `git status` right after a checkout trusts its stat cache
        and reports clean most of the time: the oracle must reread the tree (read-tree)."""
        (self.repo / "capsule").mkdir(exist_ok=True)
        for n in range(600):
            (self.repo / "capsule" / f"f{n}.py").write_text(f"line {n}\n")
        git(self.repo, "add", "capsule")
        git(self.repo, "commit", "-qm", "a tree of a realistic size")
        self.crlf("capsule")
        runs = self.env_gate(refuse=999, named=("capsule/crlf.txt",))
        seen = [self.status(self.check())["gates"] for _ in range(8)]
        self.assertEqual((seen, runs()), (["fail"] * 8, 16))

    def test_output_that_says_flaky_does_not_make_a_flaky_record(self):
        self.env_gate(refuse=0, noise="environment error, then green on retry (flaky)")
        result = self.check()
        self.assertEqual(self.status(result)["gates"], "pass")
        row, conn = self.recorded(result)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='landing_flaky'").fetchone()[0], 0)

    def test_a_wait_is_recorded_with_a_structured_cause_a_worker_verb_cannot_set(self):
        self.env_gate(refuse=2)
        row, conn = self.recorded(self.check())
        (blocked,) = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE kind='blocked'")]
        self.assertEqual((blocked["kind"], blocked["cause"]), ("transient", "lander_wait_env"))

    def test_a_third_wait_on_one_commit_opens_an_incident_the_second_does_not(self):
        self.env_gate(refuse=999)
        result = self.check()
        _, conn = self.recorded(result)
        (tid,) = [r[0] for r in conn.execute("SELECT id FROM tasks")]
        opened = []
        for now in (6, 7):
            conn.execute("UPDATE tasks SET status='done', block_kind=NULL WHERE id=?", (tid,))
            lander.record(conn, tid, result, limit=3, now=now)
            opened.append(conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0])
        self.assertEqual(opened, [0, 1])
        self.assertEqual(conn.execute("SELECT kind FROM incidents").fetchone()[0], "review_infra")

    def test_five_waits_on_one_card_commit_count_though_each_replay_has_a_new_commit(self):
        """A card behind main is replayed onto it at every check: `commit` differs each time,
        `card_commit` does not. The incident opens at the third wait, on the board named."""
        self.env_gate(refuse=999)
        result = self.check()
        _, conn = self.recorded(result)
        (tid,) = [r[0] for r in conn.execute("SELECT id FROM tasks")]
        opened = []
        for now in (6, 7, 8, 9):
            conn.execute("UPDATE tasks SET status='done', block_kind=NULL WHERE id=?", (tid,))
            replayed = dict(result, commit=f"{now:040d}", card_commit=result["card_commit"])
            lander.record(conn, tid, replayed, limit=3, now=now, board="named-board")
            opened.append(conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0])
        self.assertEqual(opened, [0, 1, 1, 1])
        self.assertEqual(conn.execute("SELECT board FROM incidents").fetchone()[0], "named-board")

    def test_a_gate_that_is_no_trusted_script_exiting_75_is_red_and_not_retried(self):
        runs = self.seen / "cfg-runs"
        self.cfg["lander"]["gates"] = [f"echo x >> {runs}; exit 75"]
        result = self.check()
        self.assertEqual((self.status(result)["gates"], result["verdict"]), ("fail", "red"))
        self.assertEqual(runs.read_text(), "x\n")

    def test_a_gate_that_judges_the_code_and_fails_is_red_without_a_retry(self):
        runs = self.env_gate(refuse=9, code=1)
        result = self.check()
        self.assertEqual((self.status(result)["gates"], result["verdict"]), ("fail", "red"))
        self.assertEqual(runs(), 1)

    def test_the_old_refusal_code_is_a_failure_not_an_environment_error(self):
        runs = self.env_gate(refuse=9, code=2)
        self.assertEqual(self.status(self.check())["gates"], "fail")
        self.assertEqual(runs(), 1)


if __name__ == "__main__":
    unittest.main()
