# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   test_a_run_that_printed_ran_n_tests_is_believed,
#   test_a_suite_that_never_ran_is_a_skip_not_unanimous,
#   test_a_summary_line_that_says_not_found_is_still_a_result,
#   test_a_test_that_fails_every_run_is_red_not_flaky,
#   test_an_already_quarantined_test_files_no_second_card,
#   test_an_unparseable_summary_is_a_skip_never_zero_killed,
#   test_an_unrecognised_test_command_writes_source_paths_only,
#   test_flake_first_night_reports_and_second_night_quarantines,
#   test_flake_infra_failure_is_a_skip_line, test_flake_runs_the_suite_five_times,
#   test_flake_timeout_is_a_skip_line, test_mutation_all_killed_opens_no_card,
#   test_mutation_infra_failure_is_a_skip_line, test_mutation_line_and_finding,
#   test_mutation_no_module_is_a_skip_line, test_mutation_runs_network_none_in_a_clone_of_main,
#   test_mutation_timeout_is_a_skip_line,
#   test_mutation_writes_source_paths_config_and_runs_plain_mutmut_run,
#   test_mutmut_absent_from_the_worker_image_is_one_skip_line,
#   test_the_config_command_branches_on_what_the_repo_already_uses,
#   test_the_config_names_pytests_test_selection,
#   test_the_mutation_phase_passes_the_repos_test_command_through,
#   test_the_test_directory_comes_out_of_the_repos_own_command
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quality_nightly as qn  # noqa: E402
from capsule.tests.nightly_support import (  # noqa: F401
    _SUITE, Tmp, _fake_repo, _repo_path, setUpModule)



class Phases(Tmp):
    def setUp(self):
        self.tmp = self.mkdtemp()
        self.state = {}

    def test_mutation_line_and_finding(self):
        def runner(repo_root, ref, cmd, **kw):
            return 0, "41/57  🎉 41 🫥 0  ⏰ 0  🤔 0  🙁 16  🔇 0"
        lines, findings = qn.phase_mutation("my-app", self.tmp, modules=["server.py"],
                                            state=self.state, runner=runner, today="2026-09-11")
        self.assertIn("mutation: my-app server.py - 41/57 killed, 16 survived", lines[0])
        self.assertEqual(findings, [("my-app", "low",
                                     "add tests that kill the 16 surviving mutants in server.py")])

    def test_mutation_all_killed_opens_no_card(self):
        def runner(*a, **kw):
            return 0, "57/57  🎉 57 🙁 0"
        lines, findings = qn.phase_mutation("my-app", self.tmp, modules=["server.py"],
                                            state=self.state, runner=runner, today="2026-09-11")
        self.assertEqual(findings, [])

    def test_mutation_timeout_is_a_skip_line(self):
        def runner(*a, **kw):
            return 124, "timed out"
        lines, findings = qn.phase_mutation("my-app", self.tmp, modules=["server.py"],
                                            state=self.state, runner=runner, today="2026-09-11")
        self.assertIn("skipped", lines[0])
        self.assertEqual(findings, [])

    def test_mutmut_absent_from_the_worker_image_is_one_skip_line(self):
        """No mutmut in the worker image: exit 127 / "not found" is a skip, not a finding."""
        for rc, out in ((127, "sh: mutmut: not found\n"),
                        (1, "/bin/sh: 1: mutmut: not found\n"),
                        (1, "No module named mutmut\n")):
            with self.subTest(rc=rc):
                lines, findings = qn.phase_mutation(
                    "my-app", self.tmp, modules=["server.py"], state={},
                    runner=lambda *a, rc=rc, out=out, **kw: (rc, out),
                    today="2026-09-11")
                self.assertEqual(lines, ["mutation: my-app server.py - "
                                         "skipped (mutmut not in the worker image)"])
                self.assertEqual(findings, [])

    def test_mutation_no_module_is_a_skip_line(self):
        def runner(*a, **kw):
            raise AssertionError("must not run the worker with nothing to mutate")
        lines, findings = qn.phase_mutation("my-app", self.tmp, modules=[],
                                            state=self.state, runner=runner, today="2026-09-11")
        self.assertEqual(lines, ["mutation: my-app - skipped (no module to mutate)"])
        self.assertEqual(findings, [])

    def test_mutation_infra_failure_is_a_skip_line(self):
        def runner(*a, **kw):
            raise qn.mg.InfraError("docker: rc 125: no daemon")
        lines, findings = qn.phase_mutation("my-app", self.tmp, modules=["server.py"],
                                            state=self.state, runner=runner, today="2026-09-11")
        self.assertIn("skipped", lines[0])
        self.assertEqual(findings, [])

    def test_mutation_writes_source_paths_config_and_runs_plain_mutmut_run(self):
        """mutmut 3.7 has no --paths-to-mutate: the module is pinned through a
        config written inside the throwaway clone."""
        seen = {}

        def runner(repo_root, ref, cmd, **kw):
            seen["cmd"] = cmd
            return 0, "57/57  \U0001f389 57 \U0001f641 0"
        qn.phase_mutation("my-app", self.tmp, modules=["src/core.py"],
                          state=self.state, runner=runner, today="2026-09-11")
        cmd = seen["cmd"]
        self.assertNotIn("--paths-to-mutate", cmd)
        self.assertIn("[mutmut]", cmd)
        self.assertIn("source_paths", cmd)
        self.assertIn("src/core.py", cmd)
        self.assertIn("setup.cfg", cmd)
        self.assertIn("[tool.mutmut]", cmd)          # the pyproject.toml branch
        # L3: file first, then tail, then re-exit with mutmut's own rc - a
        # pipeline's status is the tail's and would hide every mutmut crash.
        self.assertNotIn("| tail", cmd)
        self.assertRegex(cmd, r"mutmut run >\S+ 2>&1; rc=\$\?; tail -c \d+ \S+; exit \$rc")

    def test_the_config_command_branches_on_what_the_repo_already_uses(self):
        cmd = qn.mutmut_config_cmd("app.py")
        self.assertIn("if [ ! -f setup.cfg ]", cmd)
        self.assertIn("grep -q '^\\[mutmut\\]' setup.cfg", cmd)
        self.assertIn("pyproject.toml", cmd)

    def test_the_test_directory_comes_out_of_the_repos_own_command(self):
        """C1: mutmut 3.x has no `runner` key - it drives pytest, so the only
        way to aim it at a repo's unittest suite is the test-selection key."""
        self.assertEqual(qn.test_selection("python3 -m unittest discover maintenance/tests"),
                         "maintenance/tests")
        self.assertEqual(qn.test_selection("python3 -m unittest discover -s tests"), "tests")
        self.assertEqual(qn.test_selection("make test"), "")
        self.assertEqual(qn.test_selection(None), "")

    def test_the_config_names_pytests_test_selection(self):
        cmd = qn.mutmut_config_cmd("app.py", "python3 -m unittest discover maintenance/tests")
        self.assertIn("pytest_add_cli_args_test_selection", cmd)
        self.assertIn("'maintenance/tests'", cmd)
        self.assertNotIn("runner =", cmd)          # no such key in mutmut 3.x

    def test_an_unrecognised_test_command_writes_source_paths_only(self):
        cmd = qn.mutmut_config_cmd("app.py", "make test")
        self.assertIn("source_paths", cmd)
        self.assertNotIn("pytest_add_cli_args_test_selection", cmd)

    def test_the_mutation_phase_passes_the_repos_test_command_through(self):
        seen = {}

        def runner(repo_root, ref, cmd, **kw):
            seen["cmd"] = cmd
            return 0, "57/57  \U0001f389 57 \U0001f641 0"
        qn.phase_mutation("capsule-corp", self.tmp, modules=["app.py"], state=self.state,
                          test_cmd="python3 -m unittest discover maintenance/tests",
                          runner=runner, today="2026-09-11")
        self.assertIn("pytest_add_cli_args_test_selection", seen["cmd"])
        self.assertIn("maintenance/tests", seen["cmd"])

    def test_an_unparseable_summary_is_a_skip_never_zero_killed(self):
        lines, findings = qn.phase_mutation(
            "my-app", self.tmp, modules=["server.py"], state=self.state,
            runner=lambda *a, **kw: (0, "Traceback (most recent call last):\nRuntimeError\n"),
            today="2026-09-11")
        self.assertEqual(lines, ["mutation: my-app server.py - "
                                 "skipped (unparseable mutmut output)"])
        self.assertEqual(findings, [])
        self.assertNotIn("0/0", lines[0])
        self.assertEqual(self.state, {})              # and the rotation is held

    def test_a_summary_line_that_says_not_found_is_still_a_result(self):
        """The tightened absent check must not swallow a real run whose output
        happens to contain the words."""
        out = ("some/module.py: file not found by the coverage plugin\n"
               "41/57  \U0001f389 41 \U0001f641 16")
        lines, findings = qn.phase_mutation(
            "my-app", self.tmp, modules=["server.py"], state=self.state,
            runner=lambda *a, **kw: (0, out), today="2026-09-11")
        self.assertIn("41/57 killed, 16 survived", lines[0])
        self.assertEqual(len(findings), 1)

    def test_mutation_runs_network_none_in_a_clone_of_main(self):
        seen = {}

        def runner(repo_root, ref, cmd, **kw):
            seen.update(ref=ref, cmd=cmd, timeout=kw.get("timeout"))
            return 0, "57/57  🎉 57 🙁 0"
        qn.phase_mutation("my-app", self.tmp, modules=["server.py"],
                          state=self.state, runner=runner, today="2026-09-11")
        self.assertEqual(seen["ref"], qn.MAIN_REF)
        self.assertEqual(seen["timeout"], qn.MUTATION_TIMEOUT)
        self.assertIn("server.py", seen["cmd"])

    def test_flake_first_night_reports_and_second_night_quarantines(self):
        seq = [
            "ok\nFAILED (failures=1)\nFAIL: test_tick (tests.ui.test_soft.Soft)\n",
            "OK\n", "OK\n", "OK\n", "OK\n",
        ]

        def runner(repo_root, ref, cmd, **kw):
            return (1 if seq and "FAILED" in seq[0] else 0), seq.pop(0)

        lines, findings = qn.phase_flake("my-app", self.tmp, test_cmd=_SUITE,
                                         state=self.state, runner=runner, today="2026-09-11")
        self.assertIn("not unanimous over 5 runs", lines[0])
        self.assertEqual(findings, [])
        seq[:] = ["ok\nFAILED (failures=1)\nFAIL: test_tick (tests.ui.test_soft.Soft)\n",
                  "OK\n", "OK\n", "OK\n", "OK\n"]
        lines, findings = qn.phase_flake("my-app", self.tmp, test_cmd=_SUITE,
                                         state=self.state, runner=runner, today="2026-09-12")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], "my-app")
        self.assertEqual(findings[0][1], "med")
        self.assertIn("quarantine", findings[0][2])
        self.assertIn("flaky 2 nights", findings[0][2])

    def test_flake_runs_the_suite_five_times(self):
        calls = []

        def runner(repo_root, ref, cmd, **kw):
            calls.append((ref, cmd, kw.get("timeout")))
            return 0, "OK\n"
        lines, findings = qn.phase_flake("my-app", self.tmp, test_cmd=_SUITE,
                                         state=self.state, runner=runner, today="2026-09-11")
        self.assertEqual(len(calls), qn.FLAKE_RUNS)
        self.assertEqual({c[0] for c in calls}, {qn.MAIN_REF})
        self.assertEqual({c[2] for c in calls}, {qn.FLAKE_TIMEOUT})
        self.assertEqual(lines, ["flake: my-app - unanimous over 5 runs"])
        self.assertEqual(findings, [])

    def test_a_test_that_fails_every_run_is_red_not_flaky(self):
        def runner(*a, **kw):
            return 1, "FAIL: test_tick (tests.ui.test_soft.Soft)\nFAILED (failures=1)\n"
        lines, findings = qn.phase_flake("my-app", self.tmp, test_cmd=_SUITE,
                                         state=self.state, runner=runner, today="2026-09-11")
        self.assertEqual(lines, ["flake: my-app - unanimous over 5 runs"])
        self.assertEqual(findings, [])
        self.assertEqual(self.state, {})

    def test_flake_timeout_is_a_skip_line(self):
        def runner(*a, **kw):
            return 124, "timed out"
        lines, findings = qn.phase_flake("my-app", self.tmp, test_cmd=_SUITE,
                                         state=self.state, runner=runner, today="2026-09-11")
        self.assertIn("skipped", lines[0])
        self.assertEqual(findings, [])

    def test_flake_infra_failure_is_a_skip_line(self):
        def runner(*a, **kw):
            raise qn.mg.InfraError("docker: rc 125: no daemon")
        lines, findings = qn.phase_flake("my-app", self.tmp, test_cmd=_SUITE,
                                         state=self.state, runner=runner, today="2026-09-11")
        self.assertIn("skipped", lines[0])
        self.assertEqual(findings, [])

    def test_a_suite_that_never_ran_is_a_skip_not_unanimous(self):
        """I3: a run that dies before the first test parses as "nothing
        failed"; five of those would read `unanimous over 5 runs` every night
        and the flake policy would be silently dead."""
        cases = [(1, "Traceback (most recent call last):\nImportError: no module named app\n"),
                 (2, "usage: python -m unittest ...\n"),
                 (0, ""),
                 (127, "python3: command not found\n")]
        for rc, out in cases:
            with self.subTest(rc=rc):
                state = {}
                lines, findings = qn.phase_flake("my-app", self.tmp, test_cmd="t",
                                                 state=state,
                                                 runner=lambda *a, rc=rc, out=out, **kw: (rc, out),
                                                 today="2026-09-11")
                self.assertEqual(lines, [f"flake: my-app - skipped (suite did not run, rc {rc})"])
                self.assertEqual(findings, [])
                self.assertEqual(state, {})        # and no flake night was recorded

    def test_a_run_that_printed_ran_n_tests_is_believed(self):
        lines, _ = qn.phase_flake("my-app", self.tmp, test_cmd="t", state=self.state,
                                  runner=lambda *a, **kw: (0, "Ran 41 tests in 1.2s\n"),
                                  today="2026-09-11")
        self.assertEqual(lines, ["flake: my-app - unanimous over 5 runs"])

    def test_an_already_quarantined_test_files_no_second_card(self):
        state = {"flake_nights": {"tests.ui.test_soft.Soft.test_tick":
                                  ["2026-09-11", "2026-09-12"]},
                 "quarantined": ["tests.ui.test_soft.Soft.test_tick"]}
        seq = ["FAIL: test_tick (tests.ui.test_soft.Soft)\nFAILED (failures=1)\n",
               "OK\n", "OK\n", "OK\n", "OK\n"]

        def runner(*a, **kw):
            out = seq.pop(0)
            return (1 if "FAILED" in out else 0), out
        lines, findings = qn.phase_flake("my-app", self.tmp, test_cmd="t",
                                         state=state, runner=runner, today="2026-09-13")
        self.assertIn("not unanimous", lines[0])
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
