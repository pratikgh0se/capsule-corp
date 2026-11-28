"""R36: `main`, the container wrapper, the launchd wiring and the Z1 rows of blind_mutation. The
docker calls are stubbed; nothing here starts docker."""
import contextlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from blind_mutation_fixture import (BROKEN, CALC, ONE, RED, REPO, TWO, Base, bm,  # noqa: E402
                                    fake_run, is_pristine)

LABEL = "__PREFIX__.capsule-corp-blind-mutation"
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from capsule.tests.public_tree import private_only  # noqa: E402


def setUpModule():   # the runner never falls back to another image, so name one
    patch = mock.patch.dict(os.environ, {"OPS_WORKER_IMAGE": "stub-image:1"})
    patch.start()
    unittest.addModuleCleanup(patch.stop)


class CliBase(Base):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.config = self.home / "boards" / "b" / "config.json"
        self.config.parent.mkdir(parents=True)
        self.write_config()
        env = mock.patch.dict(os.environ, {"CAPSULE_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.run_args = []

    def write_config(self, mode="report", redesign=None, **extra):
        cfg = {"board": "b", "redesign": {"blind_tests": mode, **(redesign or {})},
               "repo_url": str(self.project), "blind_tests": {"repo": str(self.blind)}, **extra}
        self.config.write_text(json.dumps({k: v for k, v in cfg.items() if v is not None}))

    def main(self, *argv, run=fake_run):
        out, err = io.StringIO(), io.StringIO()

        def docker_run(*args):
            self.run_args.append(args)
            return run
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(bm, "_docker_run", docker_run):
            code = bm.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def reports(self):
        return list((self.home / "boards" / "b" / "blind-mutation").glob("*.json"))


class MainTests(CliBase):
    def test_a_scored_board_writes_one_report_in_its_own_folder_and_exits_zero(self):
        before = sorted(p.name for p in (self.home / "boards" / "b").iterdir())
        code, out, err = self.main("b", "--max-mutants", "2")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.strip().splitlines()), 1)
        (report,) = self.reports()
        self.assertTrue(report.name.startswith("blind-mutation-"))
        self.assertEqual(json.loads(report.read_text())["totals"]["tried"], 2)
        after = sorted(p.name for p in (self.home / "boards" / "b").iterdir())
        self.assertEqual(after, sorted(before + ["blind-mutation"]))

    def test_a_low_score_is_still_exit_zero(self):
        totals = {"score": 0.0, "killed": 0, "survived": 9, "inconclusive": 0}
        with mock.patch.object(bm, "score",
                               return_value={"truncated": False, "cards": {}, "totals": totals}):
            self.assertEqual(self.main("b")[0], 0)

    def test_every_card_baseline_erroring_exits_two_with_one_line_and_still_reports(self):
        code, out, err = self.main("b", run=lambda tree, card_dir: None)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(len(self.reports()), 1)

    def test_off_or_no_blind_repo_prints_one_skipped_line_exits_zero_and_writes_nothing(self):
        for mode, repo in (("off", True), ("report", False)):
            self.write_config(mode)
            if not repo:
                self.write_config(mode, blind_tests=None)
            code, out, err = self.main("b")
            self.assertEqual(code, 0)
            self.assertEqual(len((out + err).strip().splitlines()), 1)
            self.assertIn("b: skipped, blind tests off or no blind repo", out)
            self.assertEqual(self.reports(), [])

    def test_unknown_board_and_unreadable_repos_exit_two_with_one_line(self):
        def bad_json():
            self.config.write_text("{not json")
        cases = [
            ("ghost", lambda: None),
            ("b", lambda: self.write_config(blind_tests={"repo": str(self.tmp / "nope")})),
            ("b", lambda: self.write_config(repo_url=str(self.tmp / "no-project"))),
            ("b", bad_json),
        ]
        for board, setup in cases:
            setup()
            code, out, err = self.main(board)
            self.assertEqual(code, 2, board)
            self.assertEqual(len((out + err).strip().splitlines()), 1)
        self.assertEqual(self.reports(), [])

    def test_a_git_timeout_exits_two_with_one_line_not_a_traceback(self):
        timeout = subprocess.TimeoutExpired("git", 60)
        with mock.patch.object(bm.preflight, "git", side_effect=timeout):
            code, out, err = self.main("b")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(self.reports(), [])

    def test_sigterm_unwinds_like_an_exit_so_cleanup_runs(self):
        with self.assertRaises(SystemExit) as caught:
            bm._terminate(15, None)
        self.assertEqual(caught.exception.code, 143)


class ContainerTests(unittest.TestCase):
    def stubbed(self, exec_rc=0):
        seen = []

        def stub(argv, **kw):
            seen.append(argv)
            mounts = json.dumps([{"Mounts": [{"Type": "bind", "Source": "/w"}]}])   # R33 audit
            return subprocess.CompletedProcess(argv, exec_rc if "exec" in argv else 0,
                                               mounts if "inspect" in argv else None)
        runner = bm._TestRunner(board="b")
        runner._run, runner._sleep = stub, lambda seconds: None
        return runner, seen

    def test_the_container_is_labelled_test_not_run_and_keeps_the_strong_profile(self):
        runner, seen = self.stubbed()
        runner.start("ab" * 16, "/w", ro=True, mounts=("/s:/blind:ro",), lifetime=360)
        argv = seen[0]
        self.assertIn("capsule.kind=test", argv)
        self.assertNotIn("capsule.kind=run", argv)
        self.assertFalse([a for a in argv if a.startswith("capsule-run-id")])   # no idle-gap count
        for flag in ("--network=none", "capsule=1", "ALL", "--rm", "360"):
            self.assertIn(flag, argv)
        self.assertIn("/w:/workspace:ro", argv)

    def test_the_mount_probe_wants_the_marker_not_a_git_repo(self):
        runner, seen = self.stubbed()
        runner.start("ab" * 16, "/w", ro=True)
        probe = " ".join(next(a for a in seen if "exec" in a))
        self.assertIn(f"test -e /workspace/{bm.MARKER}", probe)
        self.assertNotIn("rev-parse", probe)

    def test_an_empty_mount_never_starts_so_it_cannot_fake_a_kill(self):
        runner, seen = self.stubbed(exec_rc=1)
        with self.assertRaises(subprocess.CalledProcessError):
            runner.start("ab" * 16, "/w", ro=True)

    def test_the_real_run_is_none_when_the_run_itself_breaks(self):
        run = bm._docker_run("/blind", "abc")
        self.assertIsNone(run(Path("/tmp"), Path("/x/not-a-card-id")))
        with mock.patch.object(bm, "_in_container", side_effect=OSError("docker gone")):
            self.assertIsNone(run(Path("/tmp"), Path("/x/t_0123abcd")))


class VerdictTests(unittest.TestCase):
    """`_in_container` over a fake container that leaves a given results.jsonl behind."""

    def verdict(self, rows, summary, code):
        tmp = Path(tempfile.mkdtemp(prefix="bm-verdict-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        tree = tmp / "tree"
        tree.mkdir()

        class Fake:
            def __init__(self, **kw):
                pass

            def start(self, run_id, ws, **kw):
                self.out = Path(kw["mounts"][1].split(":")[0])
                assert (Path(ws) / bm.MARKER).exists()
                return "capsule-x"

            def exec(self, name, line, timeout):
                (self.out / "results.jsonl").write_text(
                    "".join(json.dumps({"id": f"t{i}", "result": r}) + "\n"
                            for i, r in enumerate(rows)))
                return code, summary

            def stop(self, name):
                pass
        with mock.patch.object(bm, "_TestRunner", Fake), \
                mock.patch.object(bm.blind_run, "_snapshot"), \
                mock.patch.object(bm.blind_run, "_run_line", return_value="run"):
            return bm._in_container(tmp, tree, "/r", "c", "t_0123abcd")

    def test_pass_fail_and_a_run_that_judged_nothing(self):
        verdict = self.verdict
        self.assertIs(verdict(["pass", "pass"], "Ran 2 tests in 0.1s\n\nOK", 0), True)
        self.assertIs(verdict(["pass", "fail"], "Ran 2 tests in 0.1s\n\nFAILED (failures=1)", 1),
                      False)
        self.assertIsNone(verdict(["skipped"], "Ran 1 test in 0.1s\n\nOK (skipped=1)", 0))
        self.assertIsNone(verdict([], "Ran 0 tests in 0.0s\n\nOK", 0))


class IntentRows(CliBase):
    """Every `contracts/intent/blind_mutation.jsonl` example, replayed."""
    INTENT = REPO / "contracts" / "intent" / "blind_mutation.jsonl"

    def observe_none(self):
        doc = self.score(lambda tree, card_dir: True if is_pristine(tree) else None)
        row = doc["cards"][ONE]
        return {"killed": row["killed"], "survived": row["survived"],
                "inconclusive": row["inconclusive"], "score_null": int(row["score"] is None)}

    def observe_baseline(self):
        self.add_cards(RED, BROKEN)
        cards = self.score()["cards"]
        return {"red_baseline": cards[RED]["baseline"],
                "broken_baseline": cards[BROKEN]["baseline"],
                "mutants_tried_on_them": cards[RED]["tried"] + cards[BROKEN]["tried"],
                "healthy_card_still_scored": int(cards[TWO]["score"] == 1.0)}

    def observe_skipped(self):
        self.write_config("off")
        code, out, err = self.main("b")
        return {"exit": code, "lines": len(out.strip().splitlines()),
                "says_skipped": int("skipped" in out), "reports": len(self.reports())}

    def test_every_intent_example_replays(self):
        docs = [json.loads(ln) for ln in self.INTENT.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(docs), 3)
        for ex, observe in zip(docs, (self.observe_none, self.observe_baseline,
                                      self.observe_skipped)):
            with self.subTest(ex["id"]):
                shutil.rmtree(self.out, ignore_errors=True)
                expect = {k: v for k, v in ex["expect"].items() if k != "detail"}
                self.assertEqual(observe(), expect)


class WiringTests(unittest.TestCase):
    @private_only
    def test_the_job_is_a_weekly_plist_kept_unloaded_and_classified(self):
        plist = plistlib.loads((REPO / "launchd" / "templates" / f"{LABEL}.plist").read_bytes())
        self.assertEqual(plist["Label"], LABEL)
        self.assertEqual(plist["ProgramArguments"][-2:],
                         ["__REPO__/maintenance/blind_mutation.py", "capsule-corp"])
        self.assertEqual(set(plist["StartCalendarInterval"]), {"Weekday", "Hour", "Minute"})
        self.assertNotIn("RunAtLoad", plist)
        self.assertIn(LABEL, (REPO / "launchd" / "unloaded").read_text().splitlines())
        self.assertIn(LABEL.replace("__PREFIX__", "${_P}"),
                      (REPO / "bin" / "lib_jobs.sh").read_text())


if __name__ == "__main__":
    unittest.main()
