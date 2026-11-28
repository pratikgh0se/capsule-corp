# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   HookReposTest.test_default_does_not_guard_other_app_or_other_repos
"""tests/personal_paths_check.py, CAPSULE_WORKTREE_REPOS in bin/worktree-only-edits.py and
CAPSULE_GH_OWNER in bin/new-repo.sh (PROD-11). Everything runs the real scripts as
subprocesses in temp dirs; `gh` is a fake on PATH, never the real one."""
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import paths
from capsule.tests.public_tree import private_only

CHECK = paths.REPO / "tests" / "personal_paths_check.py"
HOOK = paths.REPO / "bin" / "worktree-only-edits.py"
NEW_REPO = paths.REPO / "bin" / "new-repo.sh"
DENYLIST = paths.REPO / "release" / "private-denylist.txt"


def _owner_name():
    """The maintainer's name as the private denylist spells it (lookbehinds stripped), or None
    when the denylist is absent (public checkout)."""
    if not DENYLIST.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "public_lint_for_paths_test", paths.REPO / "maintenance" / "public_lint.py")
    lint = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lint
    spec.loader.exec_module(lint)
    rules, _ = lint._load_denylist(DENYLIST)
    rx = next(r for category, r, _ in rules if category == "personal-name")
    return re.sub(r"\(\?<!.*?\)", "", rx)


OWNER = _owner_name()
BAD = f"x = '/Users/{OWNER}/a'\n"
OWNER_BAD = f"o = '{OWNER}gh0se'\n"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class CheckTest(unittest.TestCase):
    def setUp(self):
        if OWNER is None:
            self.skipTest("release/private-denylist.txt is absent (public checkout): "
                          "no private names to check")
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.d)])
        git(self.d, "init", "-q")

    def make(self, files, allow=None):
        for rel, text in files.items():
            (self.d / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.d / rel).write_text(text)
        if allow is not None:
            (self.d / "release").mkdir(exist_ok=True)
            (self.d / "release" / "personal-allow.txt").write_text(allow)
        git(self.d, "add", "-A")

    def run_check(self):
        p = subprocess.run([sys.executable, str(CHECK), "--repo", str(self.d)],
                           capture_output=True, text=True)
        return p.returncode, p.stdout.splitlines()

    def test_clean_repo_passes(self):
        self.make({"bin/a.sh": "echo hi\n"})
        self.assertEqual(self.run_check(), (0, ["personal_paths_check: ok"]))

    def test_both_patterns_are_reported_with_file_and_line(self):
        self.make({"bin/a.sh": "ok\n" + BAD, "capsule/b.py": OWNER_BAD})
        rc, lines = self.run_check()
        self.assertEqual(rc, 1)
        self.assertEqual(sorted(lines),
                         ["personal-path bin/a.sh:2", "personal-path capsule/b.py:1"])

    def test_out_of_scope_markdown_and_untracked_files_are_ignored(self):
        self.make({"docs/a.py": BAD, "bin/a.md": BAD})
        (self.d / "bin" / "new.sh").write_text(BAD)
        self.assertEqual(self.run_check()[0], 0)

    def test_root_scripts_are_in_scope(self):
        self.make({"deploy.sh": BAD})
        self.assertEqual(self.run_check(), (1, ["personal-path deploy.sh:1"]))

    def test_allowlisted_glob_silences_a_hit(self):
        self.make({"tests/a.py": BAD, "tests/b.py": BAD},
                  allow="# c\n\ntests/*  2026-11-08  fixture\n")
        self.assertEqual(self.run_check()[0], 0)

    def test_stale_entry_fails(self):
        self.make({"bin/a.sh": "ok\n"}, allow="bin/a.sh  2026-11-08  was a hit\n")
        self.assertEqual(self.run_check(), (1, ["allowlist-stale bin/a.sh"]))

    def test_one_live_file_does_not_keep_a_stale_glob_alive(self):
        self.make({"tests/a.py": BAD}, allow="tests/*  2026-11-08  r\nbin/*  2026-11-08  r\n")
        self.assertEqual(self.run_check(), (1, ["allowlist-stale bin/*"]))

    def test_bad_allowlist_lines_fail(self):
        for line in ("tests/a.py\n", "tests/a.py  2026-11-08\n", "tests/a.py  11-08-2026  r\n",
                     "tests/a.py  2026-13-45  r\n"):
            with self.subTest(line=line):
                self.make({"tests/a.py": BAD}, allow=line)
                rc, lines = self.run_check()
                self.assertEqual(rc, 1)
                self.assertIn("allowlist-bad release/personal-allow.txt:1", lines)

    @private_only
    def test_the_real_repo_is_clean(self):
        p = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True,
                           cwd=str(paths.REPO))
        self.assertEqual((p.returncode, p.stdout), (0, "personal_paths_check: ok\n"), p.stdout)

    @private_only
    def test_real_allowlist_stays_under_five_entries(self):
        lines = (paths.REPO / "release" / "personal-allow.txt").read_text().splitlines()
        self.assertLess(len([x for x in lines if x.strip() and not x.startswith("#")]), 5)


class HookReposTest(unittest.TestCase):
    def run_hook(self, path, repos=None, with_err=False):
        env = {k: v for k, v in os.environ.items() if k != "CAPSULE_WORKTREE_REPOS"}
        env["HOME"] = "/nonexistent-home"
        if repos is not None:
            env["CAPSULE_WORKTREE_REPOS"] = repos
        payload = {"agent_id": "a1", "tool_input": {"file_path": path}}
        p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                           capture_output=True, text=True, env=env)
        return (p.returncode, p.stderr) if with_err else p.returncode

    def test_default_guards_home_codebase_capsule_corp(self):
        self.assertEqual(self.run_hook("/nonexistent-home/CodeBase/capsule-corp/a.py"), 2)
        wt = "/nonexistent-home/CodeBase/capsule-corp/.worktrees/a.py"
        self.assertEqual(self.run_hook(wt), 0)

    def test_default_does_not_guard_other_app_or_other_repos(self):
        self.assertEqual(self.run_hook("/nonexistent-home/CodeBase/other-app/a.py"), 0)
        self.assertEqual(self.run_hook("/nonexistent-home/CodeBase/other/a.py"), 0)

    def test_env_colon_list_replaces_the_default(self):
        repos = "/srv/one:/srv/two"
        self.assertEqual(self.run_hook("/srv/one/a.py", repos), 2)
        self.assertEqual(self.run_hook("/srv/two/sub/a.py", repos), 2)
        self.assertEqual(self.run_hook("/srv/one/scratchpad/a.py", repos), 0)
        self.assertEqual(self.run_hook("/nonexistent-home/CodeBase/capsule-corp/a.py", repos), 0)

    def test_empty_env_falls_back_to_the_default(self):
        self.assertEqual(self.run_hook("/nonexistent-home/CodeBase/capsule-corp/a.py", ""), 2)

    def test_unusable_values_fall_back_to_the_default_and_warn(self):
        main = "/nonexistent-home/CodeBase/capsule-corp/a.py"
        for repos, warns in ((":", False), ("/", True), ("relative/dir", True), ("rel:/", True)):
            with self.subTest(repos=repos):
                rc, err = self.run_hook(main, repos, with_err=True)
                self.assertEqual(rc, 2)
                self.assertEqual("ignoring" in err, warns, err)
        self.assertEqual(self.run_hook("/etc/a.py", "/", with_err=True)[0], 0)

    def test_missing_path_is_kept_and_warned(self):
        rc, err = self.run_hook("/srv/gone/a.py", "/srv/gone", with_err=True)
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", err)

    def test_good_entry_survives_a_bad_one(self):
        rc, err = self.run_hook("/srv/one/a.py", "rel:/srv/one", with_err=True)
        self.assertEqual(rc, 2)
        self.assertIn("ignoring 'rel'", err)


class NewRepoOwnerTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.d)])
        (self.d / "bin").mkdir()
        gh = self.d / "bin" / "gh"
        gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$GH_LOG"\n'
                      '[ "$1 $2" = "api user" ] && printf "%s\\n" "${FAKE_LOGIN:-}"\n'
                      '[ "$1 $2" = "repo view" ] && exit 1\nexit 0\n')
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        (self.d / "home").mkdir()
        (self.d / "reg.json").write_text("[]\n")

    def run_script(self, owner=None, login=None, flags=None):
        env = {k: v for k, v in os.environ.items() if k != "CAPSULE_GH_OWNER"}
        env.update(PATH=f"{self.d / 'bin'}:{os.environ['PATH']}", GH_LOG=str(self.d / "gh.log"),
                   AGENT_OPS_HOME=str(self.d / "home"), OBSIDIAN_VAULT_PATH=str(self.d / "v"),
                   AGENT_OPS_REPOS_JSON=str(self.d / "reg.json"), CAPSULE_HOME=str(self.d / "c"),
                   GH_BIN=str(self.d / "bin" / "gh"))
        if owner:
            env["CAPSULE_GH_OWNER"] = owner
        if login:
            env["FAKE_LOGIN"] = login
        flags = flags or ["--check", "--no-github"]
        return subprocess.run(["bash", str(NEW_REPO), "widget", *flags],
                              capture_output=True, text=True, env=env)

    def gh_log(self):
        log = self.d / "gh.log"
        return log.read_text() if log.exists() else ""

    def test_no_github_uses_the_env_owner_and_makes_no_gh_call(self):
        p = self.run_script(owner="acme", login="other")
        self.assertIn("gh repo create acme/widget", p.stdout)
        self.assertEqual(self.gh_log(), "")

    def test_no_github_and_check_need_no_owner_and_no_gh_call(self):
        for flags in (["--check", "--no-github"], ["--check"]):
            with self.subTest(flags=flags):
                p = self.run_script(login="octo", flags=flags)
                self.assertNotIn("CAPSULE_GH_OWNER", p.stderr)  # (MISS rows may still exit 1)
                self.assertIn("== 7. next", p.stdout)
                self.assertEqual(self.gh_log(), "")

    def test_apply_falls_back_to_the_gh_login(self):
        p = self.run_script(login="octo", flags=["--license", "none"])
        self.assertIn("repo create octo/widget", self.gh_log())
        self.assertIn("api user -q .login", self.gh_log())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_apply_with_neither_fails_naming_the_variable_before_creating(self):
        p = self.run_script(flags=["--license", "none"])
        self.assertEqual(p.returncode, 1)
        self.assertIn("CAPSULE_GH_OWNER", p.stderr)
        self.assertNotIn("repo create", self.gh_log())


if __name__ == "__main__":
    unittest.main()
