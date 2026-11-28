# mutation-check: targets-predate-range b957739c9 -- renames only, no behaviour pinned ::
#   Guards.test_other_capsule_jobs_under_another_prefix_stop_the_run_before_any_write
"""`capsule board tick <name>`: the guards (review round 1): a harness that is not ready, a clone
that fails or is killed, the label prefix the scripts will look for, the home in the printed start
line, and `--dry-run`/`--yes` on the other `board` verbs. Same fake world as test_board_tick_setup
(temp HOME and CAPSULE_HOME, a local bare origin, a launchctl that fails if called)."""
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

from capsule import board_tick, paths
from capsule.tests.test_board_tick_setup import _World, git


class Guards(_World):
    def fake_git(self, clone_ends):
        """A `git` first on PATH whose `clone` leaves a half-made destination, then runs
        `clone_ends`; every other git command goes to the real one."""
        real = shutil.which("git", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
        script = self.bin / "git"
        script.write_text('#!/bin/sh\nif [ "$1" = clone ]; then\n  for dest; do :; done\n'
                          '  mkdir -p "$dest/.git"; echo partial > "$dest/.git/partial"\n'
                          f'  {clone_ends}\nfi\nexec {real} "$@"\n')
        script.chmod(0o755)
        return script

    def test_a_folder_with_a_git_dir_but_no_commit_is_not_ready_and_is_left_alone(self):
        self.harness.mkdir(parents=True)
        git("init", "-q", cwd=self.harness)
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 1)
        self.assertTrue([ln for ln in lines if ln.startswith("FAILED") and "no commit" in ln],
                        lines)
        self.assertTrue((self.harness / ".git").is_dir())                  # never removed: not ours
        self.assertFalse(self.plist.exists())
        self.assertNotIn("next", self.statuses(lines))

    def test_a_killed_or_failed_clone_leaves_no_folder_and_a_rerun_starts_clean(self):
        ends = {"killed": "exec sleep 30", "failed": 'echo "fatal: boom" >&2; exit 128'}
        for how, clone_ends in ends.items():
            with self.subTest(how):
                fake = self.fake_git(clone_ends)
                with mock.patch.object(board_tick, "GIT_SECONDS", 1):
                    rc, lines, _ = self.tick("--yes")
                self.assertEqual(rc, 1, lines)
                self.assertTrue([ln for ln in lines if ln.startswith("FAILED git clone")], lines)
                self.assertFalse(self.harness.exists())                    # oracle 2
                self.assertFalse(self.plist.exists())
                self.assertNotIn("next", self.statuses(lines))
                fake.unlink()
                rc, lines, _ = self.tick("--yes")                          # the rerun is clean
                self.assertEqual(rc, 0, lines)
                self.assertTrue(self.plist.is_file())
                shutil.rmtree(self.harness)
                self.plist.unlink()

    def test_an_origin_with_no_commit_is_a_failure_and_its_clone_is_removed(self):
        empty = self.tmp / "empty.git"
        git("init", "-q", "--bare", str(empty))
        git("remote", "set-url", "origin", str(empty), cwd=self.checkout)
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 1, lines)
        self.assertTrue([ln for ln in lines if "no commit" in ln], lines)
        self.assertFalse(self.harness.exists())
        self.assertFalse(self.plist.exists())

    def test_a_declined_clone_writes_no_plist_and_prints_no_start_line(self):
        rc, lines, _ = self.tick()                                         # no --yes, no answer
        self.assertEqual(rc, 0, lines)
        self.assertTrue([ln for ln in lines if ln.endswith("(declined)")])
        self.assertFalse(self.harness.exists())
        self.assertFalse(self.plist.exists())
        self.assertNotIn("next", self.statuses(lines))
        self.assertFalse([ln for ln in lines if "plist" in ln], lines)

    def test_a_relative_origin_is_read_from_the_checkout_not_from_where_it_is_run(self):
        git("remote", "set-url", "origin", "../origin.git", cwd=self.checkout)
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.harness),
                         git("rev-parse", "HEAD", cwd=self.checkout))

    def test_a_relative_capsule_home_is_made_absolute_before_the_clone_runs_in_the_checkout(self):
        work = self.tmp / "work"
        (work / "rel" / "boards" / "demo").mkdir(parents=True)
        (work / "rel" / "boards" / "demo" / "config.json").write_text('{"board": "demo"}\n')
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(work)
        listed = sorted(self.checkout.rglob("*"))
        rc, lines, _ = self.tick("--yes", home_env=False, capsule_home="rel")
        self.assertEqual(rc, 0, lines)
        self.assertTrue((work.resolve() / "rel" / "harness" / "main" / ".git").exists())
        self.assertFalse((self.checkout / "rel").exists())
        self.assertEqual(sorted(self.checkout.rglob("*")), listed)         # nothing new in REPO
        with self.plist.open("rb") as handle:
            wd = plistlib.load(handle)["WorkingDirectory"]
        self.assertTrue(os.path.isabs(wd), wd)
        self.assertEqual(Path(wd).resolve(), (work / "rel" / "harness" / "main").resolve())

    def test_a_start_line_names_the_prefix_when_it_is_not_the_default(self):
        (self.home / "host.json").write_text('{"launchd": {"label_prefix": "com.mine"}}')
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[-1], "next start it: CAPSULE_LAUNCHD_PREFIX=com.mine "
                         f"CAPSULE_HOME={shlex.quote(str(self.home))} "
                         f"{shlex.quote(str(paths.REPO / 'bin' / 'capsule-tick.sh'))} "
                         "start demo")
        (self.home / "host.json").unlink()
        (self.plist.parent / "com.mine.capsule-tick-demo.plist").unlink()
        rc, lines, _ = self.tick("--yes", extra_env={"CAPSULE_LAUNCHD_PREFIX": "dev.capsule"})
        self.assertEqual(rc, 0, lines)
        self.assertNotIn("CAPSULE_LAUNCHD_PREFIX", lines[-1])             # the default needs no env

    def test_other_capsule_jobs_under_another_prefix_stop_the_run_before_any_write(self):
        other = self.plist.parent / "com.example.capsule-tick-other.plist"
        other.parent.mkdir(parents=True)
        other.write_text("x")
        plain = ["FAILED other Capsule jobs here use prefix com.example; set "
                 "CAPSULE_LAUNCHD_PREFIX=com.example or move them"]
        for host_json in (None, '{"launchd": {"label_prefix": "com.mine"}}'):
            if host_json:
                (self.home / "host.json").write_text(host_json)
            before = self.snapshot()
            for flags in (("--yes",), ("--dry-run",)):
                with self.subTest(host_json=host_json, flags=flags):
                    rc, lines, _ = self.tick(*flags)
                    self.assertEqual((rc, lines), (1, plain))
                    self.assertEqual(self.snapshot(), before)             # nothing written
        rc, lines, _ = self.tick("--yes", extra_env={"CAPSULE_LAUNCHD_PREFIX": "com.example"})
        self.assertEqual(rc, 0, lines)                                     # the named way out
        self.assertTrue((self.plist.parent / "com.example.capsule-tick-demo.plist").is_file())
        self.assertIn("CAPSULE_LAUNCHD_PREFIX=com.example ", lines[-1])

    def test_a_bad_prefix_in_the_environment_is_refused(self):
        before = self.snapshot()
        rc, lines, _ = self.tick("--yes", extra_env={"CAPSULE_LAUNCHD_PREFIX": "a b;x"})
        self.assertEqual(rc, 1)
        self.assertTrue(lines[0].startswith("FAILED CAPSULE_LAUNCHD_PREFIX may only use"), lines)
        self.assertEqual(self.snapshot(), before)

    def test_a_snapshot_diff_counts_additions_removals_and_rewrites(self):
        one, two = {"a": ("file", "1"), "b": ("dir",)}, {"a": ("file", "2"), "c": ("dir",)}
        self.assertEqual(self.changed(one, two), {"a", "b", "c"})
        self.assertEqual(self.changed(one, one), set())
        self.assertEqual(self.changed(one, {}), {"a", "b"})                # deletions count

    def test_a_default_home_prints_a_plain_start_line(self):
        default = self.user / ".local" / "share" / "capsule"
        (default / "boards" / "demo").mkdir(parents=True)
        (default / "boards" / "demo" / "config.json").write_text('{"board": "demo"}\n')
        rc, lines, _ = self.tick("--yes", home_env=False)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(lines[-1], "next start it: " + shlex.quote(
            str(paths.REPO / "bin" / "capsule-tick.sh")) + " start demo")
        self.assertTrue((default / "harness" / "main" / ".git").exists())

    def test_dry_run_and_yes_are_refused_unless_the_verb_is_tick(self):
        env = {**os.environ, "HOME": str(self.user), "CAPSULE_HOME": str(self.home),
               "GIT_ALLOW_PROTOCOL": "file"}
        before = self.snapshot()
        for argv in (["new", "x", "--repo", "https://github.com/you/r", "--dry-run"],
                     ["new", "x", "--repo", "https://github.com/you/r", "--yes"],
                     ["--yes"], ["--dry-run"]):
            with self.subTest(argv):
                done = subprocess.run([sys.executable, "-m", "capsule", "board", *argv],
                                      cwd=paths.REPO, capture_output=True, text=True,
                                      timeout=60, env=env)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("only go with `board tick", done.stderr)
        self.assertEqual(self.snapshot(), before)                          # no board x written
