# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Constants.test_state_is_json_and_under_the_maintenance_state_dir,
#   Findings.test_it_calls_the_repos_own_intake_py_with_this_interpreter,
#   Findings.test_the_finding_line_renders_the_exact_intake_grammar,
#   Findings.test_the_intake_argv_is_exactly_the_add_finding_verb,
#   RepoModules.test_a_repo_with_no_module_is_empty,
#   Rotation.test_a_one_repo_rotation_always_picks_it,
#   Rotation.test_a_skipped_night_does_not_cost_a_module_its_turn,
#   Rotation.test_a_timeout_or_infra_error_also_holds_the_rotation,
#   Rotation.test_an_empty_module_list_is_none, Rotation.test_one_module_per_night_in_sorted_order,
#   Rotation.test_one_repo_per_night_rotating_in_order,
#   ScanPhases.test_a_repo_without_a_gitleaks_config_gets_the_default_ruleset,
#   State.test_round_trip_is_atomic_and_private
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quality_nightly as qn  # noqa: E402
from capsule.tests.nightly_support import (  # noqa: F401
    _SUITE, Tmp, _fake_repo, _repo_path, setUpModule)



class Rotation(Tmp):
    def test_one_module_per_night_in_sorted_order(self):
        mods = ["server.py", "app.py", "util.py"]
        state = {}
        picked = [qn.next_module("my-app", mods, state) for _ in range(4)]
        self.assertEqual(picked, ["app.py", "server.py", "util.py", "app.py"])

    def test_one_repo_per_night_rotating_in_order(self):
        """I1: two repos' phase timeouts do not fit the 3600s cap, so the
        repos rotate too - capsule-corp tonight, my-app tomorrow."""
        state = {}
        picked = [qn.next_repo(["capsule-corp", "my-app"], state) for _ in range(3)]
        self.assertEqual(picked, ["capsule-corp", "my-app", "capsule-corp"])

    def test_a_one_repo_rotation_always_picks_it(self):
        state = {}
        self.assertEqual([qn.next_repo(["my-app"], state) for _ in range(2)],
                         ["my-app", "my-app"])

    def test_an_empty_repo_rotation_is_none(self):
        self.assertIsNone(qn.next_repo([], {}))
        self.assertIsNone(qn.next_repo(None, {}))

    def test_a_junk_repo_index_does_not_crash(self):
        self.assertEqual(qn.next_repo(["a", "b"], {"repo_index": "nonsense"}), "a")

    def test_an_empty_module_list_is_none(self):
        self.assertIsNone(qn.next_module("my-app", [], {}))

    def test_a_skipped_night_does_not_cost_a_module_its_turn(self):
        """Two nights with mutmut absent name the same module; the first real
        run is the only thing that advances the rotation."""
        mods = ["app.py", "server.py"]
        state = {}
        tmp = self.mkdtemp()
        absent = lambda *a, **kw: (127, "sh: mutmut: not found\n")   # noqa: E731
        n1, _ = qn.phase_mutation("my-app", tmp, modules=mods, state=state,
                                  runner=absent, today="2026-09-11")
        n2, _ = qn.phase_mutation("my-app", tmp, modules=mods, state=state,
                                  runner=absent, today="2026-09-12")
        self.assertIn("app.py", n1[0])
        self.assertIn("app.py", n2[0])
        self.assertEqual(state, {})
        n3, _ = qn.phase_mutation("my-app", tmp, modules=mods, state=state,
                                  runner=lambda *a, **kw: (0, "57/57  🎉 57 🙁 0"),
                                  today="2026-09-13")
        self.assertIn("app.py", n3[0])
        n4, _ = qn.phase_mutation("my-app", tmp, modules=mods, state=state,
                                  runner=lambda *a, **kw: (0, "57/57  🎉 57 🙁 0"),
                                  today="2026-09-14")
        self.assertIn("server.py", n4[0])

    def test_a_timeout_or_infra_error_also_holds_the_rotation(self):
        mods = ["app.py", "server.py"]
        tmp = self.mkdtemp()

        def boom(*a, **kw):
            raise qn.mg.InfraError("docker: rc 125: no daemon")
        for runner in (lambda *a, **kw: (124, "timed out"), boom):
            state = {"mutation_index": 1}
            lines, _ = qn.phase_mutation("my-app", tmp, modules=mods, state=state,
                                         runner=runner, today="2026-09-11")
            self.assertEqual(state["mutation_index"], 1)


class RepoModules(Tmp):
    def test_it_walks_a_package_layout_and_skips_tests_and_glue(self):
        root = Path(self.mkdtemp())
        for rel in ("app.py", "src/__init__.py", "src/core.py", "src/db/__init__.py",
                    "src/db/models.py", "src/test_core.py", "tests/test_app.py",
                    "tests/helpers.py", ".venv/lib/thing.py", "node_modules/x/y.py",
                    "build/gen.py", "__pycache__/app.cpython-314.py", "docs/notes.md",
                    "__main__.py"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(qn.repo_modules(root),
                         ["app.py", "src/core.py", "src/db/models.py"])

    def test_a_repo_with_no_module_is_empty(self):
        root = Path(self.mkdtemp())
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(qn.repo_modules(root), [])
        self.assertIsNone(qn.next_module("my-app", qn.repo_modules(root), {}))


class State(Tmp):
    def setUp(self):
        self.own_state_dir()

    def test_round_trip_is_atomic_and_private(self):
        qn.save_state("my-app", {"mutation_index": 2, "quarantined": ["t::b"]})
        p = qn.state_path("my-app")
        self.assertEqual(qn.load_state("my-app"),
                         {"mutation_index": 2, "quarantined": ["t::b"]})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertFalse(p.with_suffix(".tmp").exists())

    def test_a_malformed_state_file_reads_as_empty(self):
        qn.state_path("capsule-corp").write_text("{not json", encoding="utf-8")
        self.assertEqual(qn.load_state("capsule-corp"), {})

    def test_a_missing_state_file_reads_as_empty(self):
        self.assertEqual(qn.load_state("no-such-repo"), {})

    def test_valid_json_that_is_not_an_object_reads_as_empty(self):
        """A list or a bare number would make every `state[...]` a TypeError on
        the next line; empty state is the only safe reading."""
        for blob in ("[1, 2]", "3", '"nope"', "null"):
            with self.subTest(blob=blob):
                qn.state_path("capsule-corp").write_text(blob, encoding="utf-8")
                self.assertEqual(qn.load_state("capsule-corp"), {})


class ParseMutmut(Tmp):
    OUT = "41/57  🎉 41 🫥 0  ⏰ 0  🤔 0  🙁 16  🔇 0"

    def test_counts(self):
        self.assertEqual(qn.parse_mutmut(self.OUT), (41, 16, 57))

    def test_unparseable_is_zeros(self):
        self.assertEqual(qn.parse_mutmut("boom"), (0, 0, 0))
        self.assertEqual(qn.parse_mutmut(""), (0, 0, 0))
        self.assertEqual(qn.parse_mutmut(None), (0, 0, 0))

    def test_the_last_progress_line_wins(self):
        """`mutmut run` rewrites its stats line in place with a carriage
        return; the tail of that stream is what the phase parses."""
        stream = ("1/57  \U0001f389 1 \U0001fae5 0  \u23f0 0  \U0001f914 0  \U0001f641 0  \U0001f507 0\r"
                  "20/57  \U0001f389 18 \U0001fae5 0  \u23f0 0  \U0001f914 0  \U0001f641 2  \U0001f507 0\r"
                  + self.OUT + "\r")
        self.assertEqual(qn.parse_mutmut(stream), (41, 16, 57))

    def test_a_killed_count_that_differs_from_the_fraction_is_believed(self):
        self.assertEqual(qn.parse_mutmut("57/57  \U0001f389 40 \u23f0 1  \U0001f914 0  \U0001f641 16"),
                         (40, 16, 57))

    def test_a_progress_line_without_the_killed_group_is_unparseable(self):
        """I5: the first number of `N/M` is mutants PROCESSED. Reporting it as
        killed invents `41/57 killed, 16 survived` out of a truncated tail and
        files a card nobody can reproduce - so no 🎉 means no stats line."""
        self.assertEqual(qn.parse_mutmut("41/57"), (0, 0, 0))
        self.assertEqual(qn.parse_mutmut("Traceback\n  File x, line 41/57\n"), (0, 0, 0))

    def test_an_ascii_summary_is_accepted_as_a_fallback(self):
        """Unverified defensive branch: mutmut 3.7 renders the emoji line, and
        this pins the ASCII shape only so an output we cannot read degrades to a
        skip rather than a wrong number."""
        self.assertEqual(qn.parse_mutmut("killed: 12  survived: 3"), (12, 3, 15))


class Flakes(Tmp):
    RUNS = [
        {"a": True, "b": True},
        {"a": True, "b": False},
        {"a": True, "b": False},
        {"a": True, "b": True},
        {"a": True, "b": True},
    ]

    def test_only_non_unanimous_tests_count(self):
        self.assertEqual(qn.parse_flakes(self.RUNS), {"b": 2})

    def test_a_unanimous_failure_is_not_a_flake(self):
        runs = [{"a": False}] * 5
        self.assertEqual(qn.parse_flakes(runs), {})

    def test_quarantine_fires_on_the_second_night_only(self):
        state = {}
        self.assertFalse(qn.quarantine(state, "t::b", "2026-09-11"))
        self.assertTrue(qn.quarantine(state, "t::b", "2026-09-12"))
        self.assertFalse(qn.quarantine(state, "t::b", "2026-09-13"))
        self.assertIn("t::b", state["quarantined"])

    def test_a_night_older_than_thirty_days_is_forgotten_before_counting(self):
        """Two flakes a quarter apart are two different bugs, not a pattern."""
        state = {"flake_nights": {"t::b": ["2026-05-01"]}}
        self.assertFalse(qn.quarantine(state, "t::b", "2026-09-11"))
        self.assertEqual(state["flake_nights"]["t::b"], ["2026-09-11"])
        self.assertEqual(state["quarantined"], [])

    def test_a_night_inside_the_window_still_counts(self):
        state = {"flake_nights": {"t::b": ["2026-08-20"]}}
        self.assertTrue(qn.quarantine(state, "t::b", "2026-09-11"))

    def test_pruning_drops_a_test_that_stopped_flaking(self):
        state = {"flake_nights": {"old::x": ["2026-01-01"], "t::b": ["2026-09-10"]}}
        qn.quarantine(state, "t::b", "2026-09-11")
        self.assertNotIn("old::x", state["flake_nights"])

    def test_a_nonsense_date_prunes_nothing_and_never_raises(self):
        state = {"flake_nights": {"t::b": ["2026-09-10"]}}
        self.assertFalse(qn.quarantine(state, "t::c", "not-a-date"))
        self.assertEqual(state["flake_nights"]["t::b"], ["2026-09-10"])


class Constants(Tmp):
    def setUp(self):
        self.own_state_dir()

    def test_the_shape_task_7_wires(self):
        self.assertEqual(qn.PHASES, ("mutation", "flake", "deps", "secrets"))
        self.assertEqual(qn.FLAKE_RUNS, 5)
        self.assertEqual(qn.MUTATION_TIMEOUT, 1500)
        self.assertEqual(qn.FINDING_MEMORY_DAYS, 14)

    def test_the_per_night_budget_fits_the_job_cap_with_margin(self):
        """run.sh kills the job at its cap; a capped run exits non-zero. ONE repo runs
        per night, so pin one repo's four phases, with room for the eight clone/`docker run`
        set-ups, not just equal to the cap."""
        self.assertEqual(qn.JOB_CAP_SECONDS, 3600)
        night = (qn.MUTATION_TIMEOUT + qn.FLAKE_RUNS * qn.FLAKE_TIMEOUT
                 + 2 * qn.SCAN_TIMEOUT)
        self.assertLessEqual(night, qn.JOB_CAP_SECONDS - qn.JOB_BUDGET_MARGIN)
        self.assertGreaterEqual(qn.JOB_BUDGET_MARGIN, 300)
        self.assertFalse(hasattr(qn, "REPOS"))   # the rotation is host.json now; two would not fit
        self.assertGreater(2 * night, qn.JOB_CAP_SECONDS)

    def test_state_is_json_and_under_the_maintenance_state_dir(self):
        p = qn.state_path("my-app")
        self.assertEqual(p.parent.name, "quality-nightly")
        self.assertTrue(str(p).startswith(os.environ["MERGE_GATE_STATE"]))
        qn.save_state("my-app", {"a": 1})
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})


class ScanPhases(Tmp):
    def test_deps_skips_without_a_tool(self):
        def runner(repo_root, ref, cmd, **kw):
            return 127, "pip-audit: not found"
        lines, findings = qn.phase_deps("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertEqual(lines, ["deps: capsule-corp - skipped (pip-audit not in the worker image)"])
        self.assertEqual(findings, [])

    def test_deps_asks_for_pip_audit_only(self):
        """One tool, one parser: an osv-scanner fallback would need a second
        output format read correctly or a scan gets misreported as clean."""
        seen = {}

        def runner(repo_root, ref, cmd, **kw):
            seen["cmd"] = cmd
            return 0, "No known vulnerabilities found"
        qn.phase_deps("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertIn("pip-audit", seen["cmd"])
        self.assertNotIn("osv-scanner", seen["cmd"])

    def test_deps_reports_and_opens_one_card(self):
        def runner(repo_root, ref, cmd, **kw):
            return 1, "Found 2 known vulnerabilities in 1 package\nrequests 2.0.0 GHSA-xxxx"
        lines, findings = qn.phase_deps("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertIn("deps: capsule-corp - 2 known vulnerabilities", lines[0])
        self.assertEqual(findings[0][1], "high")

    def test_deps_clean_opens_no_card(self):
        def runner(repo_root, ref, cmd, **kw):
            return 0, "Found 0 known vulnerabilities in 0 packages"
        lines, findings = qn.phase_deps("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertEqual(lines, ["deps: capsule-corp - no known vulnerabilities"])
        self.assertEqual(findings, [])

    def test_a_failed_audit_is_never_reported_clean(self):
        """I2: the container runs --network none, so a real pip-audit fails
        with whatever its HTTP stack says - none of which says "not found".
        A scanner that never scanned must not report clean."""
        cases = [(1, "ERROR: Connection refused"),
                 (1, "urllib3.exceptions.MaxRetryError: Max retries exceeded"),
                 (2, "Traceback (most recent call last):\nOSError: Network is unreachable"),
                 (0, "some banner and nothing else"),
                 (1, "")]
        for rc, out in cases:
            with self.subTest(rc=rc, out=out[:20]):
                lines, findings = qn.phase_deps("capsule-corp", "/tmp", today="2026-09-11",
                                                runner=lambda *a, **kw: (rc, out))
                self.assertEqual(lines, [f"deps: capsule-corp - skipped (audit failed, rc {rc})"])
                self.assertEqual(findings, [])

    def test_clean_needs_a_zero_exit_and_pip_audits_own_success_line(self):
        for out in ("No known vulnerabilities found", "Found 0 known vulnerabilities in 0 packages"):
            with self.subTest(out=out):
                lines, _ = qn.phase_deps("capsule-corp", "/tmp", today="2026-09-11",
                                         runner=lambda *a, **kw: (0, out))
                self.assertEqual(lines, ["deps: capsule-corp - no known vulnerabilities"])
        # the same success line with a non-zero exit is still a skip
        lines, _ = qn.phase_deps("capsule-corp", "/tmp", today="2026-09-11",
                                 runner=lambda *a, **kw: (1, "No known vulnerabilities found"))
        self.assertEqual(lines, ["deps: capsule-corp - skipped (audit failed, rc 1)"])

    def test_deps_infra_failure_is_a_skip_line(self):
        def runner(*a, **kw):
            raise qn.mg.InfraError("docker: rc 125: no daemon")
        lines, findings = qn.phase_deps("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertIn("skipped", lines[0])
        self.assertEqual(findings, [])

    def test_deps_runs_network_none_in_a_clone_of_main(self):
        seen = {}

        def runner(repo_root, ref, cmd, **kw):
            seen.update(ref=ref, cmd=cmd, timeout=kw.get("timeout"))
            return 0, "No known vulnerabilities found"
        qn.phase_deps("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertEqual((seen["ref"], seen["timeout"]), (qn.MAIN_REF, qn.SCAN_TIMEOUT))
        self.assertIn("pip-audit", seen["cmd"])

    def test_secrets_clean(self):
        def runner(repo_root, ref, cmd, **kw):
            return 0, "no leaks found"
        lines, findings = qn.phase_secrets("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertEqual(lines, ["secrets: capsule-corp - gitleaks clean (0 findings)"])
        self.assertEqual(findings, [])

    def test_secrets_skips_when_gitleaks_is_absent(self):
        def runner(repo_root, ref, cmd, **kw):
            return 127, "gitleaks: not found"
        lines, findings = qn.phase_secrets("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertEqual(lines, ["secrets: capsule-corp - skipped (gitleaks not on PATH)"])
        self.assertEqual(findings, [])

    def test_a_failed_scan_is_never_reported_clean(self):
        """rc != 0 means gitleaks claims leaks, so an empty, truncated or
        non-list report is a SKIP with the rc - calling a failed secret scan
        "clean" is the one outcome this phase must not produce."""
        for rc, out in ((1, "{not json"), (1, ""), (1, "   "), (2, "null"),
                        (1, "[]"), (1, '{"File": "x.py"}'), (3, "fatal: gitleaks blew up")):
            with self.subTest(rc=rc, out=out):
                lines, findings = qn.phase_secrets(
                    "capsule-corp", "/tmp", runner=lambda *a, **kw: (rc, out), today="2026-09-11")
                self.assertEqual(
                    lines,
                    [f"secrets: capsule-corp - skipped (unreadable gitleaks report, rc {rc})"])
                self.assertEqual(findings, [])

    def test_rc_zero_is_clean_whatever_it_printed(self):
        for out in ("no leaks found", "", "[]", "not json at all"):
            with self.subTest(out=out):
                lines, findings = qn.phase_secrets(
                    "capsule-corp", "/tmp", runner=lambda *a, **kw: (0, out), today="2026-09-11")
                self.assertEqual(lines, ["secrets: capsule-corp - gitleaks clean (0 findings)"])
                self.assertEqual(findings, [])

    def test_secrets_uses_the_repos_own_config(self):
        seen = {}

        def runner(repo_root, ref, cmd, **kw):
            seen.update(cmd=cmd, ref=ref, timeout=kw.get("timeout"))
            return 0, "[]"
        qn.phase_secrets("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertIn("--config .gitleaks.toml", seen["cmd"])
        self.assertTrue("--redact" in seen["cmd"] and "gitleaks git --no-banner" in seen["cmd"]
                        and "detect" not in seen["cmd"])
        self.assertEqual((seen["ref"], seen["timeout"]), (qn.MAIN_REF, qn.SCAN_TIMEOUT))

    def test_a_repo_without_a_gitleaks_config_gets_the_default_ruleset(self):
        """I6: only capsule-corp carries a .gitleaks.toml. Hard-coding --config
        made my-app's scan exit on a config error and print `skipped
        (unreadable gitleaks report)` every night, forever."""
        seen = {}

        def runner(repo_root, ref, cmd, **kw):
            seen["cmd"] = cmd
            return 0, "[]"
        qn.phase_secrets("my-app", "/tmp", runner=runner, today="2026-09-11")
        self.assertIn("[ -f .gitleaks.toml ]", seen["cmd"])     # the config is conditional
        self.assertIn("cfg=''", seen["cmd"])                    # ... and its absence is fine
        self.assertIn("--redact", seen["cmd"])                  # redaction is not

    def test_secrets_cap_is_three(self):
        report = json.dumps([{"File": f"f{i}.py", "StartLine": i} for i in range(9)])

        def runner(repo_root, ref, cmd, **kw):
            return 1, report
        lines, findings = qn.phase_secrets("capsule-corp", "/tmp", runner=runner, today="2026-09-11")
        self.assertIn("secrets: capsule-corp - gitleaks 9 findings", lines[0])
        self.assertEqual(len(findings), 3)
        self.assertEqual(findings[0][2], "fix the gitleaks finding in f0.py:0")


class GitleaksConfig(Tmp):
    """The nightly secret phase passes `--config .gitleaks.toml`, so the file
    has to exist at the repo root - and it must not itself carry a secret."""
    PATH = Path(__file__).resolve().parents[2] / ".gitleaks.toml"

    def test_it_exists_and_extends_the_default_rules(self):
        text = self.PATH.read_text(encoding="utf-8")
        self.assertIn("[extend]", text)
        self.assertIn("useDefault = true", text)

    def test_the_allowlist_is_paths_only_and_names_the_fixture_dirs(self):
        text = self.PATH.read_text(encoding="utf-8")
        self.assertIn("maintenance/tests/.*", text)
        self.assertIn("tests/.*", text)
        self.assertNotIn("regexes", text)      # a regex allowlist would embed a secret shape


class Findings(Tmp):
    def test_the_intake_argv_is_exactly_the_add_finding_verb(self):
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return 0, ""

        qn.add_finding("my-app", "med", "quarantine tests.ui.Soft.test_tick - flaky 2 nights",
                       "2026-09-12", runner=runner)
        self.assertIn("add-finding", seen[0])
        self.assertEqual(seen[0][-2:], ["--source", "quality-nightly 2026-09-12"])
        self.assertEqual(seen[0][seen[0].index("add-finding") + 1], "my-app")
        self.assertEqual(seen[0][seen[0].index("add-finding") + 2], "med")

    def test_it_calls_the_repos_own_intake_py_with_this_interpreter(self):
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return 0, ""
        qn.add_finding("my-app", "low", "add a test", "2026-09-12", runner=runner)
        self.assertEqual(seen[0][0], sys.executable)
        self.assertEqual(Path(seen[0][1]).name, "intake.py")
        self.assertTrue(Path(seen[0][1]).exists())

    def test_a_refused_line_is_reported_not_retried(self):
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return 2, "intake add-finding: no board named 'nope'\n"
        ok, msg = qn.add_finding("nope", "low", "x", "2026-09-12", runner=runner)
        self.assertFalse(ok)
        self.assertIn("no board named", msg)
        self.assertEqual(len(calls), 1)

    def test_the_finding_line_renders_the_exact_intake_grammar(self):
        import intake
        self.assertEqual(
            intake.finding_line("my-app", "med", "quarantine t::b - flaky 2 nights",
                                "quality-nightly 2026-09-12"),
            "- repo: my-app | risk: med | quarantine t::b - flaky 2 nights "
            "(quality-nightly 2026-09-12)")


if __name__ == "__main__":
    unittest.main()
