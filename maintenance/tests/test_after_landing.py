"""maintenance/after_landing.py: the post-landing chain, against temp git repos.

The fake world (Chain) and its two oracles live in after_landing_fakes.py; the intent rows are
replayed in test_after_landing_intent.py.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from after_landing_fakes import Chain, git  # noqa: E402  (also puts maintenance/ on sys.path)
import after_landing as al  # noqa: E402


class Preview(Chain):
    def test_preview_changes_nothing(self):
        self.land()
        before = self.heads()
        rc, out, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(self.heads(), before)
        self.assertIn("add capsule/x.py", out)
        self.assertIn("running ticks: alpha (pid 101), pomo (pid 303)", out)
        self.assertIn("would restart: alpha, pomo", out)
        self.assertIn("skipped: nolist", out)
        self.assertEqual([c[:2] for c in self.calls], [("deploy", ["--dry-run"])])
        self.assertEqual(self.log_rows(), [])

    def test_preview_sweeps_images_in_preview_mode(self):
        self.run_main()
        self.assertEqual(self.sweeps, [False])

    def test_preview_no_tick_code_no_restart(self):
        self.land("docs/a.md")
        _, out, _ = self.run_main()
        self.assertIn("would restart: no board", out)

    def test_preview_dry_run_failure_exits_nonzero(self):
        self.dry_rc = 2
        rc, _, err = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("./deploy.sh --dry-run failed rc=2", err)
        self.assertNotIn("re-run", err)               # a preview has nothing to re-run

    def test_preview_prints_the_stale_board_note(self):
        self.land()
        self.ticks.update(alpha=None, pomo=None)
        rc, out, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("NOTE alpha: tick not running; release clone 1 commits behind origin/main; "
                      "resume with `bin/capsule-tick.sh restart alpha`", out)
        self.assertIn("NOTE idle:", out)


class Apply(Chain):
    def test_happy_path_order_log_and_boards(self):
        self.land()
        rc, out, err = self.run_main("--apply")
        self.assertEqual((rc, err), (0, ""))
        new = git(self.origin, "rev-parse", "HEAD")
        self.assertEqual(self.heads(), (new, new))
        self.assertEqual(self.calls, [
            ("deploy", [], self.checkout), ("deploy", ["--check"], self.checkout),
            ("restart", "alpha", self.harness), ("restart", "pomo", self.harness),
            ("deploy", ["--check"], self.harness)])          # the harness check is after restart
        self.assertIn("restarted: alpha (pid 1101), pomo (pid 1303); skipped (no tick plist): "
                      "nolist", out)
        self.assertIn("after the restart", out)
        self.assertEqual([r["what"] for r in self.log_rows()], [
            "pull", "deploy", "deploy-check", "tick-restart", "tick-restart", "deploy-check"])

    def test_harness_is_fast_forwarded_when_no_tick_code_changed(self):
        self.land("docs/a.md")                                # no trigger, no restart
        rc, out, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        new = git(self.origin, "rev-parse", "HEAD")
        self.assertEqual(self.heads(), (new, new))
        self.assertEqual(self.restarts(), [])
        self.assertIn("at origin/main", out)
        pulls = [a for a in self.argvs if a[:1] == ["git"] and "pull" in a]
        self.assertEqual([a[2] for a in pulls], [str(self.checkout)])
        self.assertEqual(self.calls[-1], ("deploy", ["--check"], self.harness))

    def test_diverged_harness_refuses_when_no_restart_is_due(self):
        self.land("docs/a.md")
        self.commit(self.harness, "local.txt", "ahead")
        git(self.harness, "update-index", "--refresh")
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("git merge", err)
        self.assertEqual(self.restarts(), [])

    def test_harness_not_pulled_before_the_restart_decision(self):
        self.land()                                           # trigger: the restart moves it
        self.run_main("--apply")
        merges = [a for a in self.argvs if a[:4] == ["git", "-C", str(self.harness), "merge"]]
        self.assertEqual(merges, [])                          # only the fake restart merged


class StackRows(Chain):
    NEW = "capsule-web@sha256:" + "b" * 64
    OLD = "capsule-web@sha256:" + "a" * 64

    def setUp(self):
        super().setUp()
        self.live = self.home / "images.lock"
        self.live.write_text(f"# note\ncapsule-base@sha256:{'c' * 64}\n{self.OLD}\n"
                             f"capsule-proj-x@sha256:{'d' * 64}\n")
        self.live.chmod(0o600)
        self.docker = (0, self.NEW.split("@")[1] + "\n", "")
        real = al._run
        self.docker_calls = []

        def run(argv, cwd, timeout=300, **kw):
            if argv[0] == "docker":
                self.docker_calls.append(argv)
                self.argvs.append(argv)
                if isinstance(self.docker, Exception):
                    raise al.Refusal("docker could not run")
                return al.subprocess.CompletedProcess(argv, self.docker[0], self.docker[1],
                                                      self.docker[2])
            return real(argv, cwd, timeout)
        p = mock.patch.object(al, "_run", run)
        p.start()
        self.addCleanup(p.stop)

    def land_row(self):
        self.commit(self.origin, "docker/IMAGES.lock",
                    f"# c\ncapsule-base@sha256:{'c' * 64}\n{self.NEW}\n")

    def test_local_image_rewrites_row_keeps_others_mode_and_backup(self):
        self.land_row()
        rc, out, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.live.read_text(), f"# note\ncapsule-base@sha256:{'c' * 64}\n"
                         f"{self.NEW}\ncapsule-proj-x@sha256:{'d' * 64}\n")
        self.assertEqual(self.live.stat().st_mode & 0o777, 0o600)
        self.assertIn(self.OLD, (self.home / "images.lock.bak").read_text())
        self.assertIn("capsule-web record updated", out)
        self.assertEqual(len(self.docker_calls), 1)           # the unchanged base row: no inspect

    def test_absent_image_prints_build_line_and_leaves_record(self):
        self.land_row()
        self.docker = (1, "", "Error: No such image: sha256:bbb")
        before = self.live.read_text()
        rc, out, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.live.read_text(), before)
        self.assertIn("run docker/build-base.sh web", out)

    def test_docker_unavailable_skips_with_a_line(self):
        self.land_row()
        self.docker = Exception()
        before = self.live.read_text()
        rc, out, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.live.read_text(), before)
        self.assertIn("docker unavailable", out)

    def test_sync_runs_before_the_tick_restart_and_frees_the_lock(self):
        self.land_row()
        self.land()                                           # tick code too: ticks restart
        self.run_main("--apply")
        order = [a[0] if a[0] == "docker" else a[1] for a in self.argvs
                 if a[0] == "docker" or a[:2] == ["bin/capsule-tick.sh", "restart"]]
        self.assertEqual(order[0], "docker")
        self.assertIn("restart", order)
        self.assertFalse((self.home / "images.lock.lockdir").exists())

    def test_held_lock_waits_then_skips_cleanly(self):
        self.land_row()
        (self.home / "images.lock.lockdir").mkdir()
        before = self.live.read_text()
        with mock.patch.object(al, "LOCK_TRIES", 3), mock.patch.object(al, "LOCK_WAIT", 0):
            rc, out, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.live.read_text(), before)
        self.assertIn("record busy or unwritable", out)
        self.assertTrue((self.home / "images.lock.lockdir").exists())   # never taken over

    def test_lock_is_held_during_the_rewrite(self):
        self.land_row()
        seen = []
        real = al.os.replace
        with mock.patch.object(al.os, "replace", lambda s, d: seen.append(
                (self.home / "images.lock.lockdir").exists()) or real(s, d)):
            self.run_main("--apply")
        self.assertEqual(seen, [True])

    def test_matching_record_is_left_alone(self):
        self.live.write_text(f"capsule-base@sha256:{'c' * 64}\n{self.NEW}\n")
        self.land_row()
        self.run_main("--apply")
        self.assertEqual(self.docker_calls, [])
        self.assertFalse((self.home / "images.lock.bak").exists())


    def test_tick_code_paths_trigger_a_restart_and_others_do_not(self):
        for name, boards in (("maintenance/events.py", ["alpha", "pomo"]),
                             ("maintenance/vault_git.py", ["alpha", "pomo"]),   # boundary MAINT
                             ("maintenance/other.py", [])):
            with self.subTest(name=name):
                self.calls.clear()
                self.land(name)
                self.run_main("--apply")
                self.assertEqual(self.restarts(), boards)

    def test_deploy_check_needs_the_exact_clean_line(self):
        for out in ("deploy --check: not clean", "deploy --check: DRIFT found",
                    "all clean here", "clean"):
            with self.subTest(out=out):
                self.check_out = out
                self.assertEqual(self.run_main("--apply")[0], 1)
        self.check_out, self.check_rc = al.CLEAN, 1          # the right line but a failing rc
        self.assertEqual(self.run_main("--apply")[0], 1)
        self.check_out, self.check_rc = "x\n  " + al.CLEAN + "\n", 0
        self.assertEqual(self.run_main("--apply")[0], 0)

    def test_stopped_tick_without_tick_code_change_prints_info(self):
        self.land("docs/a.md")
        self.ticks["idle"] = None
        rc, out, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("info: tick not running for idle", out)

    def test_second_run_after_success_changes_nothing(self):
        self.land()
        self.run_main("--apply")
        self.calls.clear()
        rc, out, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        self.assertEqual(self.restarts(), [])
        self.assertIn("(unchanged)", out)

    def test_rerun_after_deploy_failure_still_restarts(self):
        self.land()
        self.deploy_rc = 3
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("already done: repo ", err)
        self.assertIn("re-run after_landing.py --apply", err)
        self.assertEqual(self.restarts(), [])
        self.deploy_rc = 0
        rc, _, _ = self.run_main("--apply")              # the repo is already pulled
        self.assertEqual((rc, self.restarts()), (0, ["alpha", "pomo"]))

    def test_observability_only_deploy_failure_still_restarts_ticks(self):
        self.land()
        self.deploy_rc = al.OBS_RC
        self.deploy_err = ("FAILED compose up\n       compose: rest failed (Bind for "
                           "127.0.0.1:3000 failed: port is already allocated)")
        rc, out, err = self.run_main("--apply")
        self.assertEqual(rc, al.OBS_RC)
        self.assertEqual(self.restarts(), ["alpha", "pomo"])
        self.assertIn("port is already allocated", err)           # loud at the failure
        self.assertIn("WARNING observability stack NOT healthy", out)   # and in the summary
        self.assertIn("port is already allocated", out)

    def test_obs_drift_on_check_after_soft_apply_still_restarts(self):
        self.land()
        self.deploy_rc, self.check_rc = al.OBS_RC, al.OBS_RC
        self.check_out = "DRIFT  capsule-corp-grafana not running"
        rc, out, err = self.run_main("--apply")
        self.assertEqual((rc, self.restarts()), (al.OBS_RC, ["alpha", "pomo"]))
        self.assertIn("WARNING observability stack NOT healthy", out)
        self.assertIn("accepted observability-only drift", out)
        self.assertNotIn("--check clean", out)
        self.assertIn("observability or secrets drift (rc=4)", err)

    def test_obs_drift_on_check_after_clean_apply_refuses(self):
        self.land()
        self.check_rc = al.OBS_RC
        self.check_out = "DRIFT  capsule-corp-grafana not running"
        rc, _, _ = self.run_main("--apply")
        self.assertEqual((rc, self.restarts()), (1, []))

    def test_essential_drift_on_check_after_soft_apply_stops(self):
        self.land()
        self.deploy_rc, self.check_rc = al.OBS_RC, 1
        self.check_out = "DRIFT  something essential"
        rc, _, _ = self.run_main("--apply")
        self.assertEqual((rc, self.restarts()), (1, []))

    def test_other_deploy_failure_codes_still_stop_before_restart(self):
        self.land()
        self.deploy_rc = 1
        rc, _, _ = self.run_main("--apply")
        self.assertEqual((rc, self.restarts()), (1, []))

    def test_dirty_harness_refuses_before_anything_moves(self):
        self.land()
        (self.harness / "stray.txt").write_text("untracked counts for the release clone")
        before = self.heads()
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("already done: nothing", err)
        self.assertEqual(self.heads(), before)
        self.assertEqual(self.calls, [])

    def test_dirty_checkout_refuses_but_untracked_is_fine(self):
        self.land()
        (self.checkout / "scratch.txt").write_text("x")
        self.assertEqual(self.run_main("--apply")[0], 0)
        self.land("docs/b.md")
        (self.checkout / "README").write_text("edited")
        before = self.heads()
        self.assertEqual(self.run_main("--apply")[0], 1)
        self.assertEqual(self.heads(), before)

    def test_diverged_checkout_refuses_and_stops(self):
        self.land()
        self.commit(self.checkout, "local.txt", "ahead")
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("git pull", err)
        self.assertEqual(self.calls, [])

    def test_check_not_clean_stops_before_restart(self):
        self.land()
        self.check_out = "drift in x"
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("deployed", err)
        self.assertEqual(self.restarts(), [])

    def test_restart_failure_stops_the_next_board(self):
        self.land()
        self.restart_rc = 1
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("tick restart for alpha not confirmed", err)
        self.assertIn("check `bin/capsule-tick.sh status alpha` first", err)
        self.assertEqual(self.restarts(), ["alpha"])

    def test_second_board_failure_names_done_and_remaining_boards(self):
        self.land()
        self.fail_on, self.restart_rc = "pomo", 1
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 1)
        self.assertEqual(self.restarts(), ["alpha", "pomo"])
        self.assertIn("restarted so far: alpha;", err)
        self.assertIn("check `bin/capsule-tick.sh status pomo` first", err)
        self.assertIn("`bin/capsule-tick.sh restart pomo --quiet 2700`", err)
        self.assertNotIn("restart alpha", err)
        partial = [r for r in self.log_rows() if r["what"] == "restart-partial"]
        self.assertEqual([r["result"] for r in partial],
                         [{"restarted": ["alpha"], "remaining": ["pomo"]}])

    def test_first_board_failure_lists_every_board_still_to_restart(self):
        self.land()
        self.fail_on, self.restart_rc = "alpha", 1
        _, _, err = self.run_main("--apply")
        self.assertIn("restarted so far: none;", err)
        self.assertIn("`bin/capsule-tick.sh restart alpha --quiet 2700`, "
                      "`bin/capsule-tick.sh restart pomo --quiet 2700`", err)

    def test_no_tick_running_but_behind_exits_zero_with_a_note(self):
        self.land()
        self.land("capsule/y.py")
        self.ticks.update(alpha=None, pomo=None)             # nothing runs; nolist has no plist
        before = git(self.harness, "rev-parse", "HEAD")
        rc, out, err = self.run_main("--apply")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.restarts(), [])
        self.assertNotIn(("deploy", ["--check"], self.harness), self.calls)
        self.assertEqual(git(self.harness, "rev-parse", "HEAD"), before)
        for board in ("alpha", "pomo", "idle"):
            self.assertIn(f"NOTE {board}: tick not running; release clone 2 commits behind "
                          f"origin/main; resume with `bin/capsule-tick.sh restart {board}`", out)
        self.assertNotIn("NOTE nolist", out)

    def test_unchanged_pid_after_restart_is_a_failure(self):
        self.land()
        self.same_pid = 1
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("not confirmed", err)


class MaintParse(Chain):
    def test_tuple_starred_and_attribute_targets_before_maint_do_not_crash(self):
        self.commit(self.origin, "capsule/boundary.py",
                    'A, B = 1, 2\n*C, D = [1, 2]\nobj = type("o", (), {})\nobj.x = 1\n'
                    'MAINT = ("maintenance/events.py",)\n')
        rc, out, err = self.run_main()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("1 tick-code file(s) differ", out)

    def test_a_missing_maint_tuple_is_a_refusal_not_a_crash(self):
        self.commit(self.origin, "capsule/boundary.py", "MAINT, OTHER = (), 1\n")
        rc, _, err = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("has no MAINT tuple", err)


class QuietWait(unittest.TestCase):
    def test_the_restart_quiet_wait_outlasts_one_contract_command(self):
        self.assertGreater(int(al.QUIET), 1200)     # tick.CONTRACT_TIMEOUT


if __name__ == "__main__":
    unittest.main()
