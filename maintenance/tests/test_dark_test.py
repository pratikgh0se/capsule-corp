# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   DarkTest.test_a_legacy_mention_that_is_not_an_error_passes,
#   DarkTest.test_exit_zero_with_a_legacy_not_found_line_still_fails
import json
import os
import plistlib
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from capsule.tests import tmpclean  # noqa: E402
tmpclean.install()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dark_test  # noqa: E402
from dark_fixture import HEAL, INCLUDED, PLISTS, DarkBase  # noqa: E402
from capsule import legacy_names as _LN  # noqa: E402

INTENT = Path(__file__).resolve().parents[2] / "contracts" / "intent" / "dark_test.jsonl"


class DarkTest(DarkBase):
    def test_start_refuses_when_legacy_home_exists_and_leaves_it_alone(self):
        (self.d / ".legacy-home").mkdir()
        (self.d / ".legacy-home" / "keep").write_text("x")
        self.assertEqual(self.run_cmd("start")[0], 2)
        self.assertEqual((self.d / ".legacy-home" / "keep").read_text(), "x")
        self.assertFalse((self.d / "state" / "dark-test" / "start.json").exists())

    def test_start_refuses_on_a_dangling_symlink_too(self):
        (self.d / ".legacy-home").symlink_to(self.d / "nowhere")
        self.assertEqual(self.run_cmd("start")[0], 2)

    def test_start_records_marker_and_baselines_and_refuses_a_second_start(self):
        (self.maint / "logs" / "launchd-heal.err").write_text("old line\n")
        (self.d / "state" / "outbox" / "1-heal.msg").write_text("x")
        base = self.started()
        marker = self.d / "state" / "dark-test" / "start.json"
        self.assertEqual(oct(marker.stat().st_mode & 0o777), "0o600")
        self.assertEqual(base["launchd_runs"][HEAL], 10)
        self.assertEqual(base["outbox"], ["1-heal.msg"])
        self.assertEqual(list(base["log_offsets"].values()), [9])
        self.assertEqual(self.run_cmd("start")[0], 2)

    def test_check_needs_a_start_marker_and_no_legacy_home(self):
        self.assertEqual(self.run_cmd("check")[0], 2)
        self.started()
        (self.d / ".legacy-home").mkdir()
        self.assertEqual(self.run_cmd("check")[0], 2)
        self.assertEqual([c for c in self.calls() if c.startswith("python3")], [])

    def test_list_marks_every_plist_with_a_reason_incl_bash_lc_forms(self):
        rc, out = self.run_cmd("list")
        self.assertEqual(rc, 0)
        lines = {ln.split()[1].rstrip(":"): ln for ln in out.splitlines()}
        for label in PLISTS:
            self.assertIn(label, lines)
            want = "included" if label in INCLUDED else "excluded"
            self.assertTrue(lines[label].startswith(want), lines[label])
            self.assertGreater(len(lines[label].split(": ", 1)[1]), 5)
        self.assertIn("run.sh", lines["dev.capsule.handoff-canary"])
        self.assertIn("autosync.sh", lines["dev.capsule.machine-setup-autosync"])
        self.assertIn("run.sh heal", lines[HEAL])
        self.assertIn("capsule-check", out)
        self.assertIn("(no log path)", lines["dev.capsule.machine-setup-autosync"])
        self.assertNotIn("(no log path)", lines[HEAL])         # it has StandardErrorPath
        self.assertNotIn("(no log path)", lines["dev.capsule.unrelated"])   # excluded: no flag

    def test_list_scans_neutral_prefix_plists_when_no_legacy_plist_exists(self):
        for p in (self.d / "LA").glob("*.plist"):
            p.unlink()
        label = "dev.capsule.capsule-corp-heal"
        (self.d / "LA" / f"{label}.plist").write_bytes(plistlib.dumps(
            {"Label": label, "ProgramArguments": ["/bin/bash", f"{self.maint}/run.sh", "heal"]}))
        rc, out = self.run_cmd("list")
        self.assertEqual(rc, 0)
        self.assertRegex(out, rf"(?m)^included\s+{re.escape(label)}:")

    def test_fixed_commands_pass_and_nothing_else_is_launched(self):
        self.started()
        self.check_heal_ok()
        self.run_cmd("check")
        calls = self.calls()
        self.assertEqual(sorted(c.split()[0] for c in calls if not c.startswith("launchctl")),
                         ["deploy.sh", "python3", "python3"])
        ctl = [c for c in calls if c.startswith("launchctl")]
        self.assertTrue(ctl and all(c.split()[1] == "print" for c in ctl))
        self.assertEqual(self.row("capsule-check")["status"], "PASS")

    def test_nonzero_exit_fails_with_last_20_stderr_lines_and_opens_a_crash_incident(self):
        self.started()
        self._set("-m_capsule_check", rc=3, err="".join(f"line {i}\n" for i in range(30)))
        self.assertEqual(self.run_cmd("check")[0], 1)
        bad = self.row("capsule-check")
        self.assertEqual((bad["status"], bad["exit"]), ("FAIL", 3))
        self.assertEqual(bad["stderr_tail"], [f"line {i}" for i in range(10, 30)])
        inc = [c for c in self.calls() if "incidents open" in c and "dark test capsule-check" in c]
        self.assertIn("--kind crash", inc[0])

    def test_nonzero_exit_keeps_stdout_fail_lines_and_names_them_in_reason(self):
        self.started()
        self._set("-m_capsule_check", rc=1, out="ok config: x\nFAIL docker: daemon not running\n")
        self.run_cmd("check")
        bad = self.row("capsule-check")
        self.assertEqual((bad["status"], bad["kind"]), ("FAIL", "crash"))
        self.assertIn("FAIL docker: daemon not running", bad["stderr_tail"])
        self.assertEqual(bad["reason"], "exit 1: FAIL docker")
        inc = [c for c in self.calls() if "incidents open" in c and "dark test capsule-check" in c]
        self.assertIn("--root-cause-key dark test capsule-check: exit 1: FAIL docker --evidence",
                      inc[0])

    def test_nonzero_exit_without_a_fail_line_keeps_the_plain_exit_reason(self):
        self.started()
        self._set("-m_capsule_check", rc=3, out="ok config: x\nnote: FAIL docker: not a row\n")
        self.run_cmd("check")
        self.assertEqual(self.row("capsule-check")["reason"], "exit 3")

    def test_exit_zero_with_a_legacy_not_found_line_still_fails(self):
        self.started()
        self._set("-m_capsule_unblocked", rc=0,
                  err=f"bash: /x/.{_LN.NAME}/venv/bin/python: No such file or directory\n")
        self.assertEqual(self.run_cmd("check")[0], 1)
        bad = self.row("capsule-unblocked")
        self.assertEqual((bad["exit"], bad["kind"]), (0, "silent_failure"))
        self.assertIn("legacy path error", bad["reason"])

    def test_unblocked_exit_1_is_info_not_fail(self):
        self.started()
        self.check_heal_ok()
        self._set("-m_capsule_unblocked", rc=1)
        self.assertEqual(self.run_cmd("check")[0], 0)
        row = self.row("capsule-unblocked")
        self.assertEqual((row["status"], row["exit"]), ("INFO", 1))
        self.assertFalse([c for c in self.calls() if "incidents open" in c])

    def test_unblocked_exit_2_still_fails(self):
        self.started()
        self._set("-m_capsule_unblocked", rc=2)
        self.assertEqual(self.run_cmd("check")[0], 1)
        self.assertEqual(self.row("capsule-unblocked")["status"], "FAIL")

    def test_deploy_check_exit_1_drift_is_info_not_crash(self):
        self.started()
        self.check_heal_ok()
        self._set("deploy", rc=1, out="DRIFT  /x/run.sh\ndeploy --check: DRIFT found\n")
        self.assertEqual(self.run_cmd("check")[0], 0)
        row = self.row("deploy-check")
        self.assertEqual((row["status"], row["exit"]), ("INFO", 1))
        self.assertIn("DRIFT  /x/run.sh", row["reason"])
        self.assertIn("DRIFT  /x/run.sh", row["stderr_tail"])
        self.assertFalse([c for c in self.calls() if "incidents open" in c])

    def test_deploy_check_exit_4_is_by_design_drift_too(self):
        self.started()
        self.check_heal_ok()
        self._set("deploy", rc=4, out="DRIFT  capsule-corp-grafana not running\n"
                  "deploy --check: DRIFT found (observability or secrets stack only)\n")
        self.assertEqual(self.run_cmd("check")[0], 0)
        row = self.row("deploy-check")
        self.assertEqual((row["status"], row["exit"]), ("INFO", 4))
        self.assertFalse([c for c in self.calls() if "incidents open" in c])

    def test_deploy_check_exit_2_still_fails(self):
        self.started()
        self._set("deploy", rc=2, out="deploy: cannot read config\n")
        self.assertEqual(self.run_cmd("check")[0], 1)
        row = self.row("deploy-check")
        self.assertEqual((row["status"], row["kind"]), ("FAIL", "crash"))
        self.assertTrue([c for c in self.calls() if "incidents open" in c and "deploy-check" in c])

    def test_a_legacy_mention_that_is_not_an_error_passes(self):
        self.started()
        self._set("-m_capsule_check", rc=0, err=f"skipped: {_LN.NAME} retired "
                  f"(.{_LN.NAME} gate off)\n")
        self.run_cmd("check")
        self.assertEqual(self.row("capsule-check")["status"], "PASS")

    def test_timeout_is_a_hang_and_kills_the_whole_process_group(self):
        self.started()
        self._set("-m_capsule_check", hang=True)
        with mock.patch.dict(os.environ, {"DARK_TEST_TIMEOUT": "1"}):
            self.run_cmd("check")
        bad = self.row("capsule-check")
        self.assertEqual((bad["status"], bad["kind"]), ("FAIL", "hang"))
        pid = int((self.d / "stubs" / "child.pid").read_text())
        for _ in range(20):          # init reaps the orphan; a plain proc.kill() leaves it 300 s
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIn("--kind hang", [c for c in self.calls() if "incidents open" in c][0])

    def test_a_failed_incident_filing_does_not_stop_the_run(self):
        self.started()
        self._set("-m_capsule_check", rc=1)
        self._set("-m_capsule_unblocked", rc=2)
        real = dark_test.subprocess.run

        def run(argv, **kw):
            if "incidents" in argv:
                raise OSError("db locked")
            return real(argv, **kw)
        with mock.patch.object(dark_test.subprocess, "run", run):
            self.assertEqual(self.run_cmd("check")[0], 1)
        for item in ("capsule-check", "capsule-unblocked"):
            self.assertTrue(self.row(item)["incident"].startswith("incident NOT opened"))
        self.assertIn("deploy-check", [r["item"] for r in self.results()])
        self.assertIn(HEAL, [r["item"] for r in self.results()])

    def test_skip_leaves_an_item_out(self):
        self.started()
        self.run_cmd("check", "--skip", HEAL, "--skip", "deploy-check")
        items = [r["item"] for r in self.results()]
        self.assertNotIn(HEAL, items)
        self.assertNotIn("deploy-check", items)

    def test_status_counts_only_results_since_start(self):
        self.started()
        self.check_heal_ok()
        res = self.d / "state" / "dark-test" / "results.jsonl"
        old = {"ts": 1, "item": "old", "status": "FAIL", "reason": "exit 1"}
        res.write_text(json.dumps(old) + "\n")
        self._set("-m_capsule_unblocked", rc=2)
        self.run_cmd("check")
        rc, out = self.run_cmd("status")
        self.assertEqual(rc, 1)
        total = 3 + len(INCLUDED)
        self.assertIn(f"{total} results", out)
        self.assertIn("FAIL capsule-unblocked", out)
        self.assertNotIn("old", out)

    def test_status_elapsed_hours_and_not_yet_due_is_no_failure(self):
        self.started(ago_s=60)
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["status"], "PASS")
        marker = self.d / "state" / "dark-test" / "start.json"
        base = json.loads(marker.read_text())
        base["start_ts"] = int(time.time()) + 1 - 5 * 3600
        marker.write_text(json.dumps(base))
        self.assertIn("5.0 h of 48 elapsed; 9 results", self.run_cmd("status")[1])

    def test_intent_examples_replay_through_judge(self):
        rows = [json.loads(ln) for ln in INTENT.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(rows), 5)
        for r in rows:
            if "board" in r["expect"]:
                continue
            status, _, kind = dark_test._judge(r["given"]["document"]["signals"])
            self.assertEqual(f"{status} {kind}" if kind else status, r["expect"]["pass"], r["id"])

    def test_intent_incident_opens_on_the_named_board(self):
        rows = [json.loads(ln) for ln in INTENT.read_text().splitlines() if ln.strip()]
        for r in (r for r in rows if "board" in r["expect"]):
            with mock.patch.object(dark_test.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="opened", stderr="")
                rec = {"item": "x", "reason": "y", "kind": "crash"}
                dark_test._open_incident(rec, Path("r.jsonl"))
            argv = run.call_args[0][0]
            self.assertEqual(argv[argv.index("--board") + 1], r["expect"]["board"], r["id"])

    def test_open_incident_gives_the_cli_a_long_busy_wait(self):
        with mock.patch.object(dark_test.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="opened", stderr="")
            dark_test._open_incident({"item": "x", "reason": "y", "kind": "crash"}, Path("r.jsonl"))
        self.assertEqual(run.call_args[1]["env"]["CAPSULE_INCIDENTS_BUSY_MS"], "100000")


if __name__ == "__main__":
    unittest.main()
