# mutation-check: targets-predate-range fc5276d0 -- rename only, no behaviour pinned ::
#   DarkPassive.test_a_log_shorter_than_the_start_offset_is_read_from_zero,
#   DarkPassive.test_agent_result_json_that_mentions_legacy_in_prose_passes,
#   DarkPassive.test_agent_result_json_with_legacy_error_fails,
#   DarkPassive.test_agent_result_json_with_legacy_in_tool_input_passes,
#   DarkPassive.test_agent_result_json_with_legacy_path_in_error_string_fails,
#   DarkPassive.test_bare_legacy_command_not_found_fails_and_outbox_na_is_recorded,
#   DarkPassive.test_hits_bounds_shell_clauses_and_scans_json_error_shapes,
#   DarkPassive.test_job_log_with_legacy_error_fails_at_launchd_exit_zero,
#   DarkPassive.test_new_outbox_message_from_the_job_fails_old_ones_do_not,
#   DarkPassive.test_plain_line_key_names_are_not_error_words,
#   DarkPassive.test_plist_log_is_scanned_only_from_the_start_offset
import datetime
import json
import os
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
import retrieval_eval_job  # noqa: E402
from dark_fixture import HEAL, PLISTS, DarkBase  # noqa: E402
from capsule import legacy_names as _LN  # noqa: E402

REAL_LAST_WAKE = getattr(dark_test, "_last_wake", None)   # the fixture patches the module name


class DarkPassive(DarkBase):
    def test_a_clean_observed_run_passes_and_cleanup_is_never_launched(self):
        self.started()
        self.check_heal_ok()
        self.run_cmd("check")
        row = self.row(HEAL)
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["signals"]["runs_since_start"], 2)
        launched = [c for c in self.calls() if not c.startswith("launchctl")]
        self.assertFalse([c for c in launched if "cleanup" in c or "run.sh" in c])

    def test_launchd_nonzero_last_exit_fails_only_if_it_ran_since_start(self):
        self.started()
        self.check_heal_ok()
        self.launchd(HEAL, runs=12, exit_code=1)
        self.run_cmd("check")
        self.assertEqual((self.row(HEAL)["status"], self.row(HEAL)["kind"]), ("FAIL", "crash"))
        self.launchd(HEAL, runs=10, exit_code=1)       # the old exit, no run since start
        self.job_state()
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["status"], "PASS")

    def test_job_log_with_legacy_error_fails_at_launchd_exit_zero(self):
        self.started()
        self.check_heal_ok()
        log = self.maint / "logs" / "heal-2026.log"
        log.write_text(f"=== heal ===\nrun.sh: /h/.{_LN.NAME}/{_LN.NAME}-agent/venv/bin/python: "
                       "not found\n")
        self.job_state(log=log)
        self.run_cmd("check")
        row = self.row(HEAL)
        self.assertEqual((row["status"], row["reason"]), ("FAIL", "legacy path error"))
        self.assertTrue(row["conflict"])
        self.assertEqual(len(row["legacy_lines"]), 1)

    def _agent_result_log(self, **fields):
        self.started()
        self.check_heal_ok()
        log = self.maint / "logs" / "heal-2026.log"
        log.write_text(json.dumps(fields) + "\n")
        self.job_state(log=log)
        self.run_cmd("check")
        return self.row(HEAL)

    def test_agent_result_json_that_mentions_legacy_in_prose_passes(self):
        row = self._agent_result_log(
            type="result", is_error=False, api_error_status=None,
            subagent_stats={"failed": 0}, permission_denials=[],
            result=f"pages still cite the archived ~/.{_LN.NAME}/kanban/boards path")
        self.assertEqual(row["status"], "PASS")
        self.assertFalse(row["legacy_lines"])

    def test_agent_result_json_with_legacy_in_tool_input_passes(self):
        row = self._agent_result_log(
            type="result", subtype="success", is_error=False, api_error_status=None,
            subagent_stats={"failed": 0},
            permission_denials=[{"tool_name": "Bash", "tool_input": {
                "command": f"git log --oneline -- {_LN.NAME}/profiles/"}}],
            result="done")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["legacy_lines"], [])
        self.assertFalse([c for c in self.calls() if "incidents open" in c])

    def test_agent_result_json_with_legacy_path_in_error_string_fails(self):
        row = self._agent_result_log(type="result", is_error=True, api_error_status=None,
                                     result=f"cannot read {_LN.NAME}/profiles/x")
        self.assertEqual((row["status"], row["reason"]), ("FAIL", "legacy path error"))

    def test_plain_line_key_names_are_not_error_words(self):
        for line in (f'{_LN.NAME} "is_error":false', f'{_LN.NAME} api_error_status=null',
                     f'{_LN.NAME} "failed":0'):
            self.assertFalse(dark_test._hits([line]), line)

    def test_agent_result_json_with_legacy_error_fails(self):
        row = self._agent_result_log(type="result", is_error=True, api_error_status=None,
                                     result=f"{_LN.NAME} send: not found")
        self.assertEqual((row["status"], row["reason"]), ("FAIL", "legacy path error"))

    def test_hits_bounds_shell_clauses_and_scans_json_error_shapes(self):
        far = f"{_LN.NAME} " + "x" * 90 + " not found"
        tool = {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": f"{_LN.NAME}: command not found"}]}}
        cases = [
            (far, False),
            (f"ModuleNotFoundError: No module named '{_LN.NAME}'", True),
            (f"{_LN.NAME} send failing", True),
            (f"{_LN.NAME} errored", True),
            (json.dumps({"is_error": True, "result": "max turns", "permission_denials": [
                {"tool_input": {"command": f"git log -- {_LN.NAME}/profiles/"}}]}), False),
            (json.dumps({"is_error": True, "event": {"tool_input": {
                "command": f"git log -- {_LN.NAME}/x"}}}), False),
            (json.dumps({"error": {"tool_input": {"command": f"{_LN.NAME}"}}}), False),
            (json.dumps({"is_error": False, "permission_denials": [
                {"tool_input": {"error": f"{_LN.NAME} x"}}]}), False),
            (f"{_LN.NAME} send: not found", True),
            (f"{{ {_LN.NAME} send: not found", True),
            (json.dumps({"error": f"{_LN.NAME} send timed out"}), True),
            (json.dumps({"errors": [f"~/.{_LN.NAME}/kanban missing"]}), True),
            (json.dumps({"error": {"failed": 0, "path": f"~/.{_LN.NAME}/x"}}), True),
            (json.dumps({"error": {"failed": 0, "path": "/tmp/x"}}), False),
            (json.dumps({"api_error_status": 500, "result": f"{_LN.NAME} down"}), True),
            (json.dumps({"is_error": True, "result": f"~/.{_LN.NAME}/kanban missing"}), True),
            (json.dumps(tool), True),
            (json.dumps({"is_error": False, "result": f"cites ~/.{_LN.NAME}/x"}), False),
        ]
        for line, hit in cases:
            with self.subTest(line=line):
                self.assertEqual(bool(dark_test._hits([line])), hit)

    def test_plist_log_is_scanned_only_from_the_start_offset(self):
        err = self.maint / "logs" / "launchd-heal.err"
        err.write_text(f"old: .{_LN.NAME} not found\n")
        self.started()
        self.check_heal_ok()
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["status"], "PASS")
        with open(err, "a") as fh:
            fh.write(f"new: {_LN.NAME.upper()}_HOME unset: error\n")
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["status"], "FAIL")

    def test_a_log_shorter_than_the_start_offset_is_read_from_zero(self):
        err = self.maint / "logs" / "launchd-heal.err"
        err.write_text("padding line so the offset is large\n" * 5)
        self.started()
        self.check_heal_ok()
        err.write_text(f"zsh: .{_LN.NAME} not found\n")       # rotated: smaller than the offset
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["reason"], "legacy path error")

    def test_bare_legacy_command_not_found_fails_and_outbox_na_is_recorded(self):
        self.started()
        self._set("-m_capsule_check", rc=0, err=f"zsh: command not found: {_LN.NAME}\n")
        self.run_cmd("check")
        self.assertEqual(self.row("capsule-check")["reason"], "legacy path error")
        keeper = self.row("dev.capsule.capsule-corp-keeper-watch")["signals"]
        self.assertIn("outbox_na", keeper)

    def test_a_standing_fault_files_one_incident_not_one_per_check(self):
        self.started()
        self._set("-m_capsule_check", rc=1)
        self.run_cmd("check")
        self.run_cmd("check")
        filing = [c for c in self.calls() if "incidents open" in c and "capsule-check" in c]
        self.assertEqual(len(filing), 1)
        self.assertTrue(self.row("capsule-check")["incident"].startswith("already filed"))
        self._set("-m_capsule_check", rc=2)               # a different fault files again
        self.run_cmd("check")
        filing = [c for c in self.calls() if "incidents open" in c and "capsule-check" in c]
        self.assertEqual(len(filing), 2)

    def test_a_nonzero_incident_filing_is_retried_and_start_resets_filed(self):
        self.started()
        self._set("-m_capsule_check", rc=1)
        self._set("-m_capsule_incidents", rc=1, err="database is locked\n")
        self.run_cmd("check")
        self.assertIn("incident NOT opened: exit 1: database is locked",
                      self.row("capsule-check")["incident"])
        (self.d / "stubs" / "-m_capsule_incidents.rc").write_text("0")
        self.run_cmd("check")
        self.assertFalse(self.row("capsule-check")["incident"].startswith("incident NOT"))
        filed = self.d / "state" / "dark-test" / "filed.json"
        self.assertIn("capsule-check|exit 1", json.loads(filed.read_text()))
        (self.d / "state" / "dark-test" / "start.json").unlink()
        self.run_cmd("start")
        self.assertEqual(json.loads(filed.read_text()), {})

    def test_an_unfiled_incident_is_retried_next_check(self):
        self.started()
        self._set("-m_capsule_check", rc=1)
        real = dark_test.subprocess.run

        def run(argv, **kw):
            if "incidents" in argv:
                raise OSError("db locked")
            return real(argv, **kw)
        with mock.patch.object(dark_test.subprocess, "run", run):
            self.run_cmd("check")
        self.run_cmd("check")
        self.assertFalse(self.row("capsule-check")["incident"].startswith("already"))

    def test_crash_of_a_job_without_a_plist_log_records_log_na(self):
        autosync = "dev.capsule.machine-setup-autosync"
        self.started()
        self.check_heal_ok()
        self.launchd(autosync, runs=11, exit_code=1)
        self.run_cmd("check")
        row = self.row(autosync)
        self.assertEqual((row["status"], row["kind"], row["reason"]), ("FAIL", "crash", "exit 1"))
        self.assertIn("StandardErrorPath", row["signals"]["log_na"])
        self.assertNotIn("log_na", self.row(HEAL)["signals"])

    def test_fork_sync_upstream_conflict_exit_is_info_not_crash(self):
        fork = "dev.capsule.fork-sync"
        with mock.patch.dict(PLISTS, {fork: (["/bin/bash", "{m}/run.sh", "fork-sync"], {})}):
            self.plist(fork)
        self.started()
        self.check_heal_ok()
        self.launchd(fork, runs=11, exit_code=3)
        self.job_state(job="fork-sync", exit_code=3)
        self.run_cmd("check")
        row = self.row(fork)
        self.assertEqual((row["status"], row["kind"]), ("INFO", None))
        self.assertIn("upstream merge conflict needs a hand merge", row["reason"])
        self.assertFalse([c for c in self.calls() if "incidents open" in c and "fork-sync" in c])
        self.launchd(fork, runs=12, exit_code=1)          # a real fault stays a crash
        self.job_state(job="fork-sync", exit_code=1)
        self.run_cmd("check")
        row = self.row(fork)
        self.assertEqual((row["status"], row["kind"]), ("FAIL", "crash"))

    def test_retrieval_eval_floor_regression_exit_is_info_not_crash(self):
        job, code = "retrieval-eval", retrieval_eval_job.FLOOR_REGRESSED_EXIT
        label = "dev.capsule.retrieval-eval"
        with mock.patch.dict(PLISTS, {label: (["/bin/bash", "{m}/run.sh", job], {})}):
            self.plist(label)
        self.started()
        self.check_heal_ok()
        self.launchd(label, runs=11, exit_code=code)
        self.job_state(job=job, exit_code=code)
        self.run_cmd("check")
        row = self.row(label)
        self.assertEqual((row["status"], row["kind"]), ("INFO", None))
        self.assertIn("recall floor", row["reason"])
        self.assertFalse([c for c in self.calls() if "incidents open" in c and job in c])
        self.launchd(label, runs=12, exit_code=1)         # a real eval failure stays a crash
        self.job_state(job=job, exit_code=1)
        self.run_cmd("check")
        row = self.row(label)
        self.assertEqual((row["status"], row["kind"]), ("FAIL", "crash"))

    def test_job_state_exit_nonzero_fails(self):
        self.started()
        self.check_heal_ok()
        self.job_state(exit_code=2)
        self.run_cmd("check")
        row = self.row(HEAL)
        self.assertEqual((row["status"], row["reason"]), ("FAIL", "exit 2"))

    def test_new_outbox_message_from_the_job_fails_old_ones_do_not(self):
        (self.d / "state" / "outbox" / "1-heal.msg").write_text("old")
        self.started()
        self.check_heal_ok()
        (self.d / "state" / "outbox" / "2-other.msg").write_text("not mine")
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["status"], "PASS")
        (self.d / "state" / "outbox" / "3-heal.msg").write_text(f"{_LN.NAME} send failed")
        self.run_cmd("check")
        row = self.row(HEAL)
        self.assertEqual((row["status"], row["reason"]), ("FAIL", "send spooled to outbox"))
        self.assertEqual(row["signals"]["outbox_new"], ["3-heal.msg"])

    def test_a_job_not_loaded_in_launchd_fails(self):
        self.started()
        self.check_heal_ok()
        (self.d / "stubs" / f"launchctl.{HEAL}").unlink()
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["reason"], "not loaded in launchd")

    def test_interval_job_with_no_run_since_start_fails_did_not_run(self):
        self.started(ago_s=3600)
        self.run_cmd("check")
        row = self.row(HEAL)
        self.assertEqual((row["status"], row["reason"], row["kind"]),
                         ("FAIL", "did not run", "silent_failure"))
        self.assertTrue([c for c in self.calls() if "incidents open" in c and "did not run" in c])

    def test_interval_counts_from_load_not_from_start(self):
        extra = {"StandardErrorPath": str(self.maint / "logs" / "launchd-heal.err")}
        self.plist(HEAL, schedule={"StartInterval": 3600}, extra=extra)
        self.started(ago_s=2 * 3600 + 900)
        self.launchd(HEAL, runs=11)
        self.job_state(age_s=25 * 60)          # last run at start + 1h50m
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["status"], "PASS")
        self.job_state(age_s=100 * 60)         # last run older than interval + grace
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["reason"], "did not run")

    def test_interval_job_quiet_only_while_host_slept_is_not_did_not_run(self):
        self.plist(HEAL, schedule={"StartInterval": 600})
        self.started(ago_s=3 * 3600)
        self.launchd(HEAL, runs=11)
        self.job_state(age_s=45 * 60)          # older than 600 + 2 * 900 s
        self.woke(120)
        self.run_cmd("check")
        row = self.row(HEAL)
        self.assertEqual(row["status"], "PASS")
        self.assertFalse(row["signals"]["did_not_run"])
        self.assertEqual(row["signals"]["last_wake"], self.wake_ts)
        self.woke(3 * 3600)                    # awake the whole quiet gap: the check still fires
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["reason"], "did not run")

    def test_short_interval_job_quiet_only_while_host_slept_is_not_did_not_run(self):
        self.plist(HEAL, schedule={"StartInterval": 600})   # the obs-probe shape
        self.started(ago_s=3 * 3600)
        self.launchd(HEAL, runs=12)
        self.job_state(age_s=3970)
        self.woke(300)
        self.run_cmd("check")
        row = self.row(HEAL)
        self.assertEqual(row["status"], "PASS")
        self.assertFalse(row["signals"]["did_not_run"])
        self.assertEqual(row["signals"]["runs_since_start"], 2)

    def test_job_with_state_quiet_only_while_host_slept_is_not_did_not_run(self):
        self.started(ago_s=3 * 3600)           # the heal shape: StartInterval 300 and a state file
        self.launchd(HEAL, runs=11)
        self.job_state(age_s=4000)             # the last run ended before the sleep
        self.woke(300)
        self.run_cmd("check")
        row = self.row(HEAL)
        self.assertFalse(row["signals"]["did_not_run"])
        self.assertEqual(row["status"], "PASS")

    def test_wake_read_failure_keeps_wall_clock_rule(self):
        self.plist(HEAL, schedule={"StartInterval": 600})
        self.started(ago_s=3 * 3600)
        self.launchd(HEAL, runs=11)
        self.job_state(age_s=45 * 60)
        self.assertEqual(self.wake_ts, 0)      # sysctl failed: nothing to discount
        self.run_cmd("check")
        self.assertEqual(self.row(HEAL)["reason"], "did not run")

    def test_last_wake_parses_sysctl_waketime_and_returns_0_on_any_error(self):
        out = "{ sec = 1793000000, usec = 123456 } Mon Oct 26 08:03:20 2026\n"
        ok = mock.Mock(returncode=0, stdout=out)
        with mock.patch.object(dark_test.subprocess, "run", return_value=ok) as run:
            self.assertEqual(REAL_LAST_WAKE(), 1793000000)
        self.assertEqual(run.call_args.args[0], ["sysctl", "-n", "kern.waketime"])
        junk = mock.Mock(returncode=0, stdout="unreadable\n")
        for result in ({"return_value": junk}, {"side_effect": OSError("no sysctl")},
                       {"return_value": mock.Mock(returncode=1, stdout=out)}):
            with mock.patch.object(dark_test.subprocess, "run", **result):
                self.assertEqual(REAL_LAST_WAKE(), 0)

    def test_calendar_job_due_in_the_window_must_have_run(self):
        due = datetime.datetime.now() - datetime.timedelta(hours=2)
        sched = {"StartCalendarInterval": {"Hour": due.hour, "Minute": due.minute}}
        label = "dev.capsule.obsidian-report"
        self.plist(label, schedule=sched)
        self.started(ago_s=3 * 3600)
        self.run_cmd("check")
        self.assertEqual(self.row(label)["reason"], "did not run")
        self.job_state("report")
        self.plist(label, schedule=sched, extra={"StandardOutPath": str(self.d / "report.out")})
        (self.d / "report.out").write_text("ran\n")
        self.run_cmd("check")
        self.assertEqual(self.row(label)["status"], "PASS")

    def test_weekday_schedule_before_start_is_not_due(self):
        today = (datetime.datetime.now().weekday() + 1) % 7
        sched = {"StartCalendarInterval": {"Weekday": (today + 3) % 7, "Hour": 2, "Minute": 3}}
        self.plist("dev.capsule.obsidian-report", schedule=sched)
        self.started(ago_s=3600)
        self.run_cmd("check")
        self.assertEqual(self.row("dev.capsule.obsidian-report")["status"], "PASS")

    def test_calendar_job_that_joined_after_start_is_not_due_for_a_slot_before_it_joined(self):
        due = datetime.datetime.now() - datetime.timedelta(hours=2)
        sched = {"StartCalendarInterval": {"Hour": due.hour, "Minute": due.minute}}
        label = "dev.capsule.obsidian-report"
        (self.d / "LA" / f"{label}.plist").unlink()
        self.started(ago_s=3 * 3600)               # no plist for the label at start
        self.plist(label, schedule=sched)
        self.run_cmd("check")
        row = self.row(label)
        self.assertEqual(row["status"], "PASS")
        self.assertFalse(row["signals"]["did_not_run"])
        joined = self.d / "state" / "dark-test" / "joined.json"
        first = json.loads(joined.read_text())[label]
        self.run_cmd("check")
        self.assertEqual(json.loads(joined.read_text())[label], first)   # written once
        joined.write_text(json.dumps({label: int(time.time()) - 3 * 3600}))   # before the slot
        self.run_cmd("check")
        self.assertEqual(self.row(label)["reason"], "did not run")
        (self.d / "state" / "dark-test" / "start.json").unlink()
        self.run_cmd("start")
        self.assertEqual(json.loads(joined.read_text()), {})

    def _weekly(self, loaded_s_ago, slot_s_ago=2 * 3600):
        slot = datetime.datetime.now() - datetime.timedelta(seconds=slot_s_ago)
        day = (slot.weekday() + 1) % 7
        sched = {"StartCalendarInterval": {"Weekday": day, "Hour": slot.hour,
                                           "Minute": slot.minute}}
        label = "dev.capsule.obsidian-report"
        self.plist(label, schedule=sched)
        loaded = time.time() - loaded_s_ago
        os.utime(self.d / "LA" / f"{label}.plist", (loaded, loaded))
        self.started(ago_s=3 * 86400)
        self.run_cmd("check")
        return self.row(label)

    def test_weekly_job_loaded_after_its_last_slot_is_not_did_not_run(self):
        row = self._weekly(loaded_s_ago=3600)            # slot 2h ago, loaded 1h ago
        self.assertEqual(row["signals"]["runs_since_start"], 0)
        self.assertFalse(row["signals"]["did_not_run"])
        self.assertEqual(row["status"], "PASS")

    def test_weekly_job_loaded_before_its_slot_that_missed_it_is_did_not_run(self):
        row = self._weekly(loaded_s_ago=3 * 3600)        # slot 2h ago, loaded 3h ago
        self.assertEqual(row["reason"], "did not run")


if __name__ == "__main__":
    unittest.main()
