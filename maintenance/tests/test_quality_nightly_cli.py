# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Cli.test_a_dry_run_does_not_advance_the_rotation,
#   Cli.test_a_real_run_adds_the_finding_through_intake_and_saves_state,
#   Cli.test_an_unknown_phase_name_is_dropped, Cli.test_dry_run_writes_nothing,
#   Cli.test_it_never_blocks, Cli.test_one_repo_runs_per_night_and_the_rotation_advances,
#   Dedupe.test_a_dry_run_leaves_the_memory_on_disk_untouched,
#   Dedupe.test_a_filing_expires_after_fourteen_days_and_is_pruned,
#   Dedupe.test_a_finding_is_remembered_once_filed,
#   Dedupe.test_a_junk_filed_entry_is_dropped_not_a_crash,
#   Dedupe.test_the_fifteenth_night_files_it_again, Dedupe.test_the_key_is_repo_phase_and_title,
#   Dedupe.test_the_secret_phase_dedupes_per_file_and_line,
#   Dedupe.test_two_consecutive_nights_file_one_finding,
#   RunRepo.test_a_repo_with_no_checkout_is_one_skip_line,
#   RunRepo.test_an_unknown_phase_raises_inside_the_wrapper,
#   RunRepo.test_one_broken_phase_does_not_stop_the_others
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quality_nightly as qn  # noqa: E402
from capsule.tests.nightly_support import (  # noqa: F401
    _SUITE, Tmp, _fake_repo, _repo_path, setUpModule)



class Dedupe(Tmp):
    def test_a_finding_is_remembered_once_filed(self):
        state = {}
        self.assertFalse(qn.filed_recently(state, "deps", "my-app", "upgrade x", "2026-09-11"))
        qn.mark_filed(state, "deps", "my-app", "upgrade x", "2026-09-11")
        self.assertTrue(qn.filed_recently(state, "deps", "my-app", "upgrade x", "2026-09-12"))

    def test_the_key_is_repo_phase_and_title(self):
        state = {}
        qn.mark_filed(state, "deps", "my-app", "upgrade x", "2026-09-11")
        for phase, repo, title in (("secrets", "my-app", "upgrade x"),
                                   ("deps", "capsule-corp", "upgrade x"),
                                   ("deps", "my-app", "upgrade y")):
            with self.subTest(phase=phase, repo=repo, title=title):
                self.assertFalse(qn.filed_recently(state, phase, repo, title, "2026-09-12"))

    def test_a_filing_expires_after_fourteen_days_and_is_pruned(self):
        key = qn._filed_key("deps", "my-app", "upgrade x")
        state = {"filed": {key: "2026-09-11"}}
        self.assertTrue(qn.filed_recently(state, "deps", "my-app", "upgrade x", "2026-09-25"))
        self.assertFalse(qn.filed_recently(state, "deps", "my-app", "upgrade x", "2026-09-26"))
        self.assertEqual(state["filed"], {})       # pruned, so the memory cannot grow forever

    def test_a_junk_filed_entry_is_dropped_not_a_crash(self):
        state = {"filed": {"deps|my-app|x": None, "deps|my-app|y": 7}}
        self.assertFalse(qn.filed_recently(state, "deps", "my-app", "x", "2026-09-12"))
        self.assertEqual(state["filed"], {})

    def test_two_consecutive_nights_file_one_finding(self):
        """The whole point: the deps phase reports the same vulnerable package
        every night, and Intake must get one line, not thirty."""
        root = _fake_repo(self)

        def runner(repo_root, ref, cmd, **kw):
            return 1, "Found 2 known vulnerabilities in 1 package"
        state = {}
        with _repo_path("my-app", root):
            n1, f1 = qn.run_repo("my-app", state=state, runner=runner,
                                 today="2026-09-11", phases=("deps",))
            n2, f2 = qn.run_repo("my-app", state=state, runner=runner,
                                 today="2026-09-12", phases=("deps",))
        self.assertEqual(len(f1), 1)
        self.assertEqual(f2, [])
        self.assertEqual(n1, ["deps: my-app - 2 known vulnerabilities"])
        self.assertEqual(n2, ["deps: my-app - 2 known vulnerabilities",
                              "deps: my-app - already filed within 14 days: "
                              "upgrade the 2 dependencies with known vulnerabilities"])

    def test_the_fifteenth_night_files_it_again(self):
        root = _fake_repo(self)

        def runner(repo_root, ref, cmd, **kw):
            return 1, "Found 2 known vulnerabilities in 1 package"
        state = {}
        with _repo_path("my-app", root):
            _, f1 = qn.run_repo("my-app", state=state, runner=runner,
                                today="2026-09-11", phases=("deps",))
            _, f2 = qn.run_repo("my-app", state=state, runner=runner,
                                today="2026-09-26", phases=("deps",))
        self.assertEqual(len(f1), 1)
        self.assertEqual(len(f2), 1)

    def test_the_secret_phase_dedupes_per_file_and_line(self):
        root = _fake_repo(self)
        report = json.dumps([{"File": "a.py", "StartLine": 3}, {"File": "b.py", "StartLine": 9}])

        def runner(repo_root, ref, cmd, **kw):
            return 1, report
        state = {}
        with _repo_path("my-app", root):
            _, f1 = qn.run_repo("my-app", state=state, runner=runner,
                                today="2026-09-11", phases=("secrets",))
            _, f2 = qn.run_repo("my-app", state=state, runner=runner,
                                today="2026-09-12", phases=("secrets",))
        self.assertEqual(len(f1), 2)
        self.assertEqual(f2, [])

    def test_a_dry_run_leaves_the_memory_on_disk_untouched(self):
        """A dry night must not suppress the card a real night would file."""
        self.own_state_dir()
        root = _fake_repo(self)
        qn.state_path("my-app").unlink(missing_ok=True)

        def runner(repo_root, ref, cmd, **kw):
            return 1, "Found 2 known vulnerabilities in 1 package"
        buf = io.StringIO()
        with _repo_path("my-app", root), contextlib.redirect_stdout(buf):
            qn.main(["--repos", "my-app", "--phases", "deps", "--dry-run"], runner=runner)
        self.assertFalse(qn.state_path("my-app").exists())
        self.assertIn("would add:", buf.getvalue())


class RunRepo(Tmp):
    def test_a_repo_with_no_checkout_is_one_skip_line(self):
        lines, findings = qn.run_repo("no-such-repo", state={}, today="2026-09-11")
        self.assertEqual(len(lines), 1)
        self.assertIn("skipped (no checkout", lines[0])
        self.assertEqual(findings, [])

    def test_an_unknown_phase_raises_inside_the_wrapper(self):
        """run_repo names every phase explicitly: a typo is a loud ValueError
        turned into one line, never a silent fall-through to the secret scan."""
        root = _fake_repo(self)

        def runner(*a, **kw):
            raise AssertionError("no phase should have run")
        with _repo_path("my-app", root):
            lines, findings = qn.run_repo("my-app", state={}, runner=runner,
                                          today="2026-09-11", phases=("nonsense",))
        self.assertEqual(len(lines), 1)
        self.assertIn("unknown phase 'nonsense'", lines[0])
        self.assertEqual(findings, [])

    def test_one_broken_phase_does_not_stop_the_others(self):
        root = _fake_repo(self)

        def runner(repo_root, ref, cmd, **kw):
            if "gitleaks" in cmd:
                raise RuntimeError("boom")
            return 0, "No known vulnerabilities found"
        with _repo_path("my-app", root):
            lines, findings = qn.run_repo("my-app", state={}, runner=runner,
                                          today="2026-09-11", phases=("deps", "secrets"))
        self.assertEqual(lines[0], "deps: my-app - no known vulnerabilities")
        self.assertIn("secrets: my-app - skipped (boom)", lines[1])
        self.assertEqual(findings, [])


class Cli(Tmp):
    def setUp(self):
        self.own_state_dir()

    def test_dry_run_writes_nothing(self):
        seen = []

        def finding_runner(argv, **kw):
            seen.append(argv)
            return 0, ""

        def runner(repo_root, ref, cmd, **kw):
            return 1, "Found 2 known vulnerabilities in 1 package"

        root = _fake_repo(self)
        qn.state_path("my-app").unlink(missing_ok=True)
        buf = io.StringIO()
        with _repo_path("my-app", root), contextlib.redirect_stdout(buf):
            out = qn.main(["--repos", "my-app", "--phases", "deps", "--dry-run"],
                          runner=runner, finding_runner=finding_runner)
        self.assertEqual(out, 0)
        self.assertEqual(seen, [])
        self.assertFalse(qn.state_path("my-app").exists())
        self.assertIn("would add: - repo: my-app | risk: high | "
                      "upgrade the 2 dependencies with known vulnerabilities "
                      "(quality-nightly ", buf.getvalue())

    def test_a_real_run_adds_the_finding_through_intake_and_saves_state(self):
        seen = []

        def finding_runner(argv, **kw):
            seen.append(argv)
            return 0, ""

        def runner(repo_root, ref, cmd, **kw):
            return 1, "Found 2 known vulnerabilities in 1 package"

        root = _fake_repo(self)
        qn.state_path("my-app").unlink(missing_ok=True)
        buf = io.StringIO()
        with _repo_path("my-app", root), contextlib.redirect_stdout(buf):
            out = qn.main(["--repos", "my-app", "--phases", "deps"],
                          runner=runner, finding_runner=finding_runner)
        self.assertEqual(out, 0)
        self.assertEqual(len(seen), 1)
        self.assertIn("add-finding", seen[0])
        self.assertIn("finding: added", buf.getvalue())
        self.assertTrue(qn.state_path("my-app").exists())

    def test_an_unknown_phase_name_is_dropped(self):
        calls = []

        def runner(repo_root, ref, cmd, **kw):
            calls.append(cmd)
            return 0, "No known vulnerabilities found"
        root = _fake_repo(self)
        buf = io.StringIO()
        with _repo_path("my-app", root), contextlib.redirect_stdout(buf):
            qn.main(["--repos", "my-app", "--phases", "deps,nonsense", "--dry-run"],
                    runner=runner)
        self.assertEqual(len(calls), 1)

    def test_one_repo_runs_per_night_and_the_rotation_advances(self):
        """I1: the whole point of the fix - `--repos a,b` is a rotation, not
        two runs in one night."""
        roots = {}
        for repo in ("capsule-corp", "my-app"):
            roots[repo] = _fake_repo(self)
        seen = []

        def runner(repo_root, ref, cmd, **kw):
            seen.append(str(repo_root))
            return 0, "No known vulnerabilities found"
        buf = io.StringIO()
        with _repo_path("capsule-corp", roots["capsule-corp"]), \
                _repo_path("my-app", roots["my-app"]), \
                contextlib.redirect_stdout(buf):
            qn.main(["--repos", "capsule-corp,my-app", "--phases", "deps"], runner=runner)
            first = buf.getvalue()
            qn.main(["--repos", "capsule-corp,my-app", "--phases", "deps"], runner=runner)
        both = buf.getvalue()
        self.assertEqual(seen, [roots["capsule-corp"], roots["my-app"]])
        self.assertIn("quality-nightly: capsule-corp tonight", first)
        self.assertIn("deps: capsule-corp - no known vulnerabilities", first)
        self.assertNotIn("my-app", first)
        self.assertIn("quality-nightly: my-app tonight", both[len(first):])
        self.assertEqual(json.loads(qn.state_path(qn.ROTATION_STATE).read_text())["repo_index"], 0)

    def test_a_dry_run_does_not_advance_the_rotation(self):
        root = _fake_repo(self)
        qn.state_path(qn.ROTATION_STATE).unlink(missing_ok=True)
        with _repo_path("my-app", root), contextlib.redirect_stdout(io.StringIO()):
            qn.main(["--repos", "capsule-corp,my-app", "--phases", "deps", "--dry-run"],
                    runner=lambda *a, **kw: (0, "No known vulnerabilities found"))
        self.assertFalse(qn.state_path(qn.ROTATION_STATE).exists())

    def test_it_never_blocks(self):
        def runner(*a, **kw):
            raise qn.mg.InfraError("docker: rc 125: no daemon")
        root = _fake_repo(self)
        buf = io.StringIO()
        with _repo_path("my-app", root), contextlib.redirect_stdout(buf):
            self.assertEqual(
                qn.main(["--repos", "my-app", "--phases", "deps,secrets", "--dry-run"],
                        runner=runner), 0)


if __name__ == "__main__":
    unittest.main()
