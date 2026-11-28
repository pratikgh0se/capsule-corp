# mutation-check: targets-predate-range b957739c9 -- renames only, no behaviour pinned ::
#   Full.test_other_repos_are_skipped_and_the_default_night_excludes_it,
#   Rotation.test_default_phases_still_rotate
"""quality_nightly's `full` phase (D273): the whole suites a selected landing skipped, all faked."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quality_nightly as qn  # noqa: E402
import merge_gate as mg  # noqa: E402


def fake(results):
    seen = []

    def runner(root, ref, cmd, **kw):
        seen.append(cmd)
        res = results[len(seen) - 1]
        if isinstance(res, Exception):
            raise res
        return res, ""
    return runner, seen


class Full(unittest.TestCase):
    def setUp(self):
        """No live board config: the example, so the suite count and names are fixed."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        real = qn._full_suites
        patch = mock.patch.object(qn, "_full_suites", lambda home=None: real(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_all_green_runs_the_boards_four_suites_and_files_nothing(self):
        runner, seen = fake([0, 0, 0, 0])
        lines, findings = qn._phase_full("capsule-corp", "/x", runner=runner)
        self.assertEqual(len(seen), len(qn._full_suites()))
        self.assertIn("discover -s capsule/tests", seen[0])
        self.assertTrue(all(line.endswith("green") for line in lines))
        self.assertEqual(findings, [])

    def test_a_red_suite_is_one_finding_and_the_rest_still_run(self):
        runner, seen = fake([1, 0, 0, 0])
        lines, findings = qn._phase_full("capsule-corp", "/x", runner=runner)
        self.assertEqual(len(seen), 4)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][:2], ("capsule-corp", "high"))
        self.assertIn("full capsule suite red", findings[0][2])
        self.assertIn("red (rc 1)", lines[0])

    def test_an_infra_failure_is_a_skip_not_a_finding(self):
        runner, _ = fake([mg.InfraError("no docker"), 0, 0, 0])
        lines, findings = qn._phase_full("capsule-corp", "/x", runner=runner)
        self.assertIn("skipped (no docker)", lines[0])
        self.assertEqual(findings, [])

    def test_other_repos_are_skipped_and_the_default_night_excludes_it(self):
        def boom(*a, **kw):
            raise AssertionError("must not run")
        lines, _ = qn._phase_full("sample-app", "/x", runner=boom)
        self.assertIn("skipped", lines[0])
        self.assertNotIn("full", qn.PHASES)

    def test_an_unusable_board_config_is_a_finding_not_a_quiet_skip(self):
        def boom(home=None):
            raise ValueError("broken config")
        def never(*a, **kw):
            raise AssertionError("no suite list, nothing to run")
        with mock.patch.object(qn, "_full_suites", boom):
            lines, findings = qn._phase_full("capsule-corp", "/x", runner=never)
        self.assertIn("board config unusable", lines[0])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][:2], ("capsule-corp", "medium"))
        self.assertIn("board config", findings[0][2])

    def test_a_timeout_is_a_skip_line_not_a_finding(self):
        runner, _ = fake([124, 0, 0, 0])
        lines, findings = qn._phase_full("capsule-corp", "/x", runner=runner)
        self.assertIn("timed out", lines[0])
        self.assertEqual(findings, [])

    def test_no_suite_giving_a_verdict_is_one_finding_not_a_pass(self):
        down = mg.InfraError("docker down")
        runner, _ = fake([down] * 4)
        lines, findings = qn._phase_full("capsule-corp", "/x", runner=runner)
        self.assertEqual(len(findings), 1)
        self.assertIn("no verdict", findings[0][2])
        runner, _ = fake([124, 124, 124, 124])
        self.assertEqual(len(qn._phase_full("capsule-corp", "/x", runner=runner)[1]), 1)


class InStepWithTheBoard(unittest.TestCase):
    def home(self, cfg=None):
        """A temp capsule home holding the board's config (the example, with one command added)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        text = json.loads(qn.EXAMPLE_CONFIG.read_text())
        text["contract_commands"] = [*text["contract_commands"], "echo board-only-command"]
        board = Path(tmp.name) / "boards" / qn.BOARD
        board.mkdir(parents=True)
        (board / "config.json").write_text(json.dumps(text) if cfg is None else cfg)
        return tmp.name

    def test_the_nightly_runs_exactly_what_the_lander_resolves_for_the_board(self):
        from capsule import config, paths
        home = self.home()
        lander_cmds = config.load(paths.resolve_config(Path(home), qn.BOARD))["contract_commands"]
        self.assertEqual([c for _, c in qn._full_suites(home)], [f"{c} 2>&1" for c in lander_cmds])
        self.assertIn("echo board-only-command 2>&1", [c for _, c in qn._full_suites(home)])

    def test_a_missing_board_config_falls_back_to_the_example_and_says_so(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stderr(err):
            got = qn._full_suites(empty)
        example = json.loads(qn.EXAMPLE_CONFIG.read_text())["contract_commands"]
        self.assertEqual([c for _, c in got], [f"{c} 2>&1" for c in example])
        self.assertIn("using the example config", err.getvalue())

    def test_the_example_keeps_the_slow_shard_limit_and_the_no_send_setting(self):
        cmds = json.loads(qn.EXAMPLE_CONFIG.read_text())["contract_commands"]
        self.assertTrue(any("SHELL_SUITE_TIMEOUT=720" in c for c in cmds))
        self.assertTrue(any("AGENT_OPS_NO_SEND=1" in c for c in cmds))


class Rotation(unittest.TestCase):
    """Z1 intent: `--phases full --repos capsule-corp` names its repo and never moves the default
    nightly's rotation index."""

    def run_main(self, argv):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, CAPSULE_HOME=d, MERGE_GATE_STATE=d), \
                mock.patch.object(qn, "run_repo", return_value=([], [])) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            qn.save_state(qn.ROTATION_STATE, {"repo_index": 1})
            qn.main(argv)
            return run, qn.load_state(qn.ROTATION_STATE)

    def test_full_run_names_its_repo_and_keeps_the_rotation(self):
        run, rotation = self.run_main(["--repos", "capsule-corp", "--phases", "full"])
        self.assertEqual(run.call_args.args[0], "capsule-corp")
        self.assertEqual(rotation, {"repo_index": 1})

    def test_default_phases_still_rotate(self):
        _, rotation = self.run_main(["--repos", "capsule-corp,sample-app", "--phases", "deps"])
        self.assertNotEqual(rotation, {"repo_index": 1})


if __name__ == "__main__":
    unittest.main()
