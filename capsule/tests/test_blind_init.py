# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   GlobGuard.test_overlap_handles_the_filesystem_root_and_case
"""R13: `capsule blind init`, the board's `blind_tests` block and the blind preflight. Everything
runs against temp paths: no remote repo, no network, no real board."""
import contextlib
import io
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import __main__ as capsule_main
from capsule import config, paths
from capsule.blind import cli, preflight

EXAMPLE_TEXT = (paths.REPO / "capsule" / "config.example.json").read_text()   # board "my-app"
BLOCK = {"repo": "/tmp/x-blind", "adapter": "unittest", "tester_may_read": ["README.md"],
         "interface_commands": ["python3 -m capsule --help"]}
INTENT_FILE = paths.INTENT / "blind_harness.jsonl"
INTENT_IDS = {"tester-glob-reaching-production-is-refused"}
# Globs that could reach production code, history or secrets; every one must be refused.
REFUSED_GLOBS = (
    "capsule/**", "src/app.py", "lib/**", "bin/x", "tests/**", "*", "**", "**/*.md", "c*/x",
    "/etc/passwd", "~/x", "docs/../capsule/x.py", "./capsule/x", "././capsule/tick.py",
    ".//capsule/**", "Capsule/**", "CAPSULE/tick.py", ".git/**", ".git/objects/**", ".env",
    "docs/.env", "{capsule,docs}/**", " capsule/x", "capsule", "[c]apsule/x", "docs//x", "docs/",
    "a\\b", "docs/\nx",
)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = capsule_main.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class BlindConfig(unittest.TestCase):
    def cfg(self, **block):
        return {**json.loads(EXAMPLE_TEXT), "blind_tests": block}

    def test_absent_block_reads_as_none_and_validates(self):
        base = json.loads(EXAMPLE_TEXT)
        config.validate(base)
        self.assertIsNone(config.blind_tests(base))

    def test_block_gets_defaults_and_is_a_copy(self):
        block = config.blind_tests(self.cfg(repo="/r", adapter="unittest"))
        self.assertEqual((block["tester_may_read"], block["interface_commands"]), ([], []))
        block["tester_may_read"].append("x")
        again = config.blind_tests(self.cfg(repo="/r", adapter="unittest"))
        self.assertEqual(again["tester_may_read"], [])

    def test_validation(self):
        config.validate(self.cfg(**BLOCK))
        for bad in ({"adapter": "unittest"}, {"repo": "/r"}, {"repo": "/r", "adapter": "rspec"},
                    {"repo": "", "adapter": "unittest"},
                    {"repo": "/r", "adapter": "unittest", "x": 1},
                    {"repo": "/r", "adapter": "unittest", "tester_may_read": "docs"},
                    {"repo": "/r", "adapter": "unittest", "interface_commands": [""]},
                    {"repo": 3, "adapter": "unittest"}, {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate(self.cfg(**bad))
        with self.assertRaises(ValueError):
            config.validate({**json.loads(EXAMPLE_TEXT), "blind_tests": "yes"})


class GlobGuard(unittest.TestCase):
    def test_docs_and_schemas_pass(self):
        for glob in ("README.md", "docs/RUNBOOK.md", "contracts/schemas/**", "app/**"):
            with self.subTest(glob=glob):
                self.assertIsNone(preflight._glob_problem(glob))

    def test_production_history_and_escaping_globs_are_refused(self):
        for glob in REFUSED_GLOBS:
            with self.subTest(glob=glob):
                self.assertTrue(preflight._glob_problem(glob))

    def test_default_may_read_is_the_readme_only(self):
        self.assertEqual(cli.DEFAULT_MAY_READ, ("README.md",))

    def test_overlap_handles_the_filesystem_root_and_case(self):
        self.assertTrue(preflight._overlaps(Path("/"), Path("/home/x")))
        self.assertTrue(preflight._overlaps(Path("/home/x"), Path("/")))
        self.assertTrue(preflight._overlaps(Path("/A/b"), Path("/a")))
        self.assertFalse(preflight._overlaps(Path("/a/b"), Path("/a/bc")))


class BlindTestCase(unittest.TestCase):
    """A temp CAPSULE_HOME with one board config copied from the example, and a temp repo root."""
    board = "my-app"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.cfg_path = paths.config_path(self.home, self.board)
        self.cfg_path.parent.mkdir(parents=True)
        self.cfg_path.write_text(EXAMPLE_TEXT)
        self.repo = self.tmp / "b1-blind"

    def init(self, *extra, repo=None, board=None):
        return run_cli("blind", "init", board or self.board, "--repo", str(repo or self.repo),
                       "--capsule-home", str(self.home), *extra)

    def refuses(self, word, *extra, **kw):
        code, _, err = self.init(*extra, **kw)
        self.assertEqual(code, 1, err)
        self.assertRegex(err, word)

    def git(self, *args):
        done = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        return done.stdout

    def loaded(self):
        return config.load(self.cfg_path)

    def preflight_problems(self, cfg=None, mounts=()):
        return preflight.problems(cfg or self.loaded(), self.board, mounts)


class InitBoard(BlindTestCase):
    def test_creates_repo_with_one_commit_and_toml_that_passes_preflight(self):
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(sorted(p.name for p in self.repo.iterdir() if p.name != ".git"),
                         [".gitignore", "README.md", "blind.toml", "cards", "interface"])
        self.assertEqual(len(self.git("log", "--format=%s").splitlines()), 1)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.preflight_problems(), [])

    def test_run_line_starts_in_the_snapshot_not_the_workspace(self):
        self.init()
        toml = (self.repo / "blind.toml").read_text()
        self.assertIn('run = "cd /blind && exec python3 -m harness.unittest_runner', toml)
        self.assertIn('list = "cd /blind && exec python3 -m harness.unittest_runner', toml)
        self.assertIn("--list", toml)
        self.assertNotIn("PYTHONPATH", toml)       # `cd /blind` already puts /blind on the path

    def test_config_gets_the_block_and_keeps_the_rest_of_the_file_byte_for_byte(self):
        before = self.cfg_path.read_text()
        self.init("--may-read", "README.md", "--interface-command", "x --help")
        after = self.cfg_path.read_text()
        self.assertTrue(after.startswith(before.rstrip()[:-1].rstrip()))
        block = config.blind_tests(json.loads(after))
        self.assertEqual((block["adapter"], block["tester_may_read"], block["interface_commands"]),
                         ("unittest", ["README.md"], ["x --help"]))
        self.assertEqual(json.loads(after)["redesign"], json.loads(before)["redesign"])

    def test_config_file_mode_is_kept(self):
        self.cfg_path.chmod(0o600)
        self.init()
        self.assertEqual(stat.S_IMODE(self.cfg_path.stat().st_mode), 0o600)

    def test_second_run_changes_nothing(self):
        self.init()
        cfg_after, head = self.cfg_path.read_text(), self.git("rev-parse", "HEAD")
        code, out, _ = self.init()
        self.assertEqual(code, 0)
        now = (self.cfg_path.read_text(), self.git("rev-parse", "HEAD"))
        self.assertEqual(now, (cfg_after, head))
        self.assertIn("already present", out)

    def test_dry_run_writes_nothing(self):
        before = self.cfg_path.read_text()
        code, out, _ = self.init("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.repo.exists())
        self.assertEqual(self.cfg_path.read_text(), before)
        self.assertIn("would create", out)

    def test_never_overwrites_an_existing_blind_toml_or_test(self):
        self.init()
        (self.repo / "cards" / "t_x").mkdir()
        (self.repo / "cards" / "t_x" / "test_a.py").write_text("# landed test\n")
        toml = (self.repo / "blind.toml").read_text()
        self.cfg_path.write_text(EXAMPLE_TEXT)
        self.assertEqual(self.init()[0], 0)    # block gone from the config, repo present
        self.assertEqual((self.repo / "blind.toml").read_text(), toml)
        self.assertEqual((self.repo / "cards" / "t_x" / "test_a.py").read_text(), "# landed test\n")

    def test_refusals_leave_nothing_behind(self):
        (self.tmp / "other").mkdir()
        (self.tmp / "other" / "f").write_text("x")
        (self.tmp / "gitless").mkdir()
        before = self.cfg_path.read_text()
        board_work = paths.board_dir(self.board, self.home) / "work" / "r"
        deps = Path.home() / ".local" / "share" / "capsule-corp" / "deps" / "r"
        cases = [(["--may-read", "capsule/**"], {}, "production"),
                 (["--adapter", "rspec"], {}, "one of"),
                 (["--adapter", "pytest"], {}, "no harness runner"),
                 (["--may-read", ".git/**"], {}, "hidden"),
                 ([], {"repo": self.tmp / "other"}, "not this board's blind repo"),
                 ([], {"repo": board_work}, "mounts"), ([], {"repo": deps}, "mounts"),
                 ([], {"repo": "relative/path"}, "absolute"), ([], {"repo": "/"}, "mounts"),
                 ([], {"board": "../evil"}, "letters, digits")]
        for extra, kw, word in cases:
            with self.subTest(extra=extra, kw=kw):
                self.refuses(word, *extra, **kw)
        self.assertFalse(self.repo.exists())
        self.assertEqual(self.cfg_path.read_text(), before)

    def test_a_repo_directory_without_a_commit_is_not_accepted(self):
        self.repo.mkdir()
        (self.repo / "blind.toml").write_text(cli._toml(self.board, "unittest"))
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.refuses("git repository")
        self.assertEqual(self.git("log", "--format=%s", "-1"), "")

    def test_a_config_that_would_not_validate_is_refused_before_the_repo_is_made(self):
        with mock.patch.object(cli, "_config_with_block", side_effect=ValueError("nope")):
            self.refuses("nope")
        self.assertFalse(self.repo.exists())

    def test_refuses_a_different_existing_block_and_a_missing_or_mismatched_config(self):
        self.init()
        self.refuses("different blind_tests", repo=self.tmp / "elsewhere")
        self.refuses("board config", board="nope")
        cfg = json.loads(EXAMPLE_TEXT)
        cfg["board"] = "other"
        self.cfg_path.write_text(json.dumps(cfg))
        self.refuses("not 'my-app'", repo=self.tmp / "third")
        self.assertFalse((self.tmp / "third").exists())


class Preflight(BlindTestCase):
    def test_no_block_means_unavailable(self):
        self.assertEqual(self.preflight_problems(), ["the board has no blind_tests block"])

    def test_missing_repo(self):
        self.init("--dry-run")
        cfg = {**self.loaded(), "blind_tests": {**BLOCK, "repo": str(self.repo)}}
        self.assertEqual(self.preflight_problems(cfg),
                         [f"the blind repo {preflight.expand(self.repo)} does not exist"])

    def test_bad_toml(self):
        self.init()
        (self.repo / "blind.toml").write_text(
            'version = 2\nboard = "other"\nadapter = "pytest"\nrun = ""\n')
        found = " | ".join(self.preflight_problems())
        for word in ("version must be 1", "board is 'other'", "differs from", "list must"):
            self.assertIn(word, found)
        (self.repo / "blind.toml").write_text("not = [toml")
        self.assertIn("unreadable", " ".join(self.preflight_problems()))

    def write_run_line(self, line):
        """Rewrite the `run` line of the repo's blind.toml; the rest stays as init wrote it."""
        text = (self.repo / "blind.toml").read_text()
        old = f"run = {json.dumps(cli.RUN_LINE)}\n"
        self.assertIn(old, text)
        (self.repo / "blind.toml").write_text(text.replace(old, f"run = {json.dumps(line)}\n"))

    def test_the_safe_run_line_is_the_one_constant_init_writes(self):
        self.init()
        self.assertIs(cli.RUN_LINE, preflight.RUN_LINE)
        self.assertEqual(self.preflight_problems(), [])

    @staticmethod
    def dodges(safe):
        """Run lines that try to look like `safe`: stale, wrapped, chained, padded, altered."""
        return [
            safe.replace("exec ", ""),
            f"sh -c {json.dumps(safe)}",
            f"bash -lc '{safe}'",
            f"/bin/sh -c '{safe}'",
            f"{safe}; true",
            f"{safe} && true",
            f"{safe} | cat",
            f"{safe} || true",
            f"true; {safe}",
            f"true && {safe}",
            f"echo hi | {safe}",
            f"FOO=1 {safe}",
            f"PYTHONPATH=/workspace {safe}",
            f"{safe} trailing",
            f"{safe} ",
            f" {safe}",
            f"{safe}\n",
            f"{safe}\nrm -rf /",
            f"{safe} &",
            f"{safe} $(true)",
            safe.replace("/blind/cards/{card}", "/workspace/cards/{card}"),
            safe.replace("cd /blind", "cd /workspace"),
            safe.replace("cd /blind && ", ""),
            safe.replace("cd /blind && exec", "cd /blind  && exec"),
            safe.replace("--card {card}", "--card {card} --extra"),
            safe.replace("python3", "python3 -S"),
            "exec python3 -m harness.unittest_runner --card {card} /blind/cards/{card}",
            "echo ok",
        ]

    def test_any_run_line_but_the_safe_form_is_a_problem_naming_the_line_and_the_fix(self):
        self.init()
        safe = cli.RUN_LINE
        for line in self.dodges(safe):
            with self.subTest(line=line):
                self.write_run_line(line)
                found = self.preflight_problems()
                self.assertEqual(len(found), 1, found)
                self.assertIn(repr(line), found[0])
                self.assertIn(repr(safe), found[0])
                self.assertIn("run", found[0])
                (self.repo / "blind.toml").write_text(cli._toml(self.board, "unittest"))

    def test_a_run_line_that_is_not_text_or_is_empty_keeps_its_own_message(self):
        self.init()
        text = (self.repo / "blind.toml").read_text()
        old = f"run = {json.dumps(cli.RUN_LINE)}\n"
        for value in ('""', "5", "[1]"):
            with self.subTest(value=value):
                (self.repo / "blind.toml").write_text(text.replace(old, f"run = {value}\n"))
                self.assertEqual(self.preflight_problems(), ["blind.toml: run must be a command"])

    def test_a_stale_run_line_makes_the_cli_preflight_exit_1(self):
        self.init()
        self.write_run_line(cli.RUN_LINE.replace("exec ", ""))
        code, _, err = run_cli("blind", "preflight", self.board, "--capsule-home", str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("blind: unavailable", err)
        self.assertIn("exec", err)

    def test_init_does_not_call_a_repo_with_a_stale_run_line_ready(self):
        self.init()
        self.write_run_line(cli.RUN_LINE.replace("exec ", ""))
        self.refuses("run line")

    def commit_run_line(self, line):
        """Commit blind.toml with `line` as its run line, then put the safe working copy back."""
        self.write_run_line(line)
        preflight.git(self.repo, "add", "-A")
        preflight.git(self.repo, "commit", "-q", "-m", "run line")
        (self.repo / "blind.toml").write_text(cli._toml(self.board, "unittest"))

    def test_a_stale_run_line_at_head_is_a_problem_though_the_working_copy_is_fixed(self):
        self.init()
        stale = cli.RUN_LINE.replace("exec ", "")
        self.commit_run_line(stale)
        found = self.preflight_problems()
        self.assertEqual(len(found), 1, found)
        for word in ("HEAD", repr(stale), repr(cli.RUN_LINE), "Commit"):
            self.assertIn(word, found[0])
        code, _, err = run_cli("blind", "preflight", self.board, "--capsule-home", str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("HEAD", err)
        self.refuses("HEAD")

    def test_a_stale_run_line_in_both_places_is_reported_for_each(self):
        self.init()
        self.commit_run_line(cli.RUN_LINE.replace("exec ", ""))
        self.write_run_line("echo ok")
        found = self.preflight_problems()
        self.assertEqual([("HEAD" in p) for p in found], [False, True])

    def test_a_stale_run_line_only_in_an_older_commit_does_not_count(self):
        self.init()
        self.commit_run_line(cli.RUN_LINE.replace("exec ", ""))
        self.commit_run_line(cli.RUN_LINE)
        self.assertEqual(self.preflight_problems(), [])
        self.assertEqual(self.init()[0], 0)

    def toml_text(self, adapter, run):
        return (f'version = 1\nboard = "{self.board}"\nadapter = {adapter}\n'
                f"run = {json.dumps(run)}\nlist = \"x\"\n")

    def test_an_adapter_with_no_runner_is_refused_whatever_the_line(self):
        self.init()
        for line in (cli.RUN_LINE, "echo ok", "exec python3 -m pytest {card}"):
            with self.subTest(line=line):
                (self.repo / "blind.toml").write_text(self.toml_text('"pytest"', line))
                found = preflight.toml_problems(self.repo / "blind.toml", self.board, "pytest")
                self.assertTrue(any("no blind runner" in p for p in found), found)
        self.assertIn("no blind runner", preflight.run_line_problem(cli.RUN_LINE, "pytest"))

    def test_a_list_valued_adapter_is_a_clean_problem_not_an_exception(self):
        self.init()
        self.assertIsInstance(preflight.run_line_problem(cli.RUN_LINE, ["unittest"]), str)
        (self.repo / "blind.toml").write_text(self.toml_text('["unittest"]', cli.RUN_LINE))
        found = preflight.toml_problems(self.repo / "blind.toml", self.board, "unittest")
        self.assertTrue(any("adapter must be one of" in p for p in found), found)
        preflight.git(self.repo, "add", "-A")
        preflight.git(self.repo, "commit", "-q", "-m", "list adapter")
        (self.repo / "blind.toml").write_text(cli._toml(self.board, "unittest"))
        found = self.preflight_problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn("HEAD", found[0])
        self.assertIn("no blind runner", found[0])

    def test_init_says_the_run_line_is_stale_then_gives_the_fix(self):
        self.init()
        self.write_run_line(cli.RUN_LINE.replace("exec ", ""))
        code, _, err = self.init()
        self.assertEqual(code, 1)
        self.assertNotIn("not this board's", err)
        self.assertLess(err.index("stale"), err.index(repr(cli.RUN_LINE)))

    def test_a_blind_toml_that_is_not_in_the_head_commit_is_a_problem(self):
        self.init()
        preflight.git(self.repo, "rm", "-q", "--cached", "blind.toml")
        preflight.git(self.repo, "commit", "-q", "-m", "untrack")
        found = self.preflight_problems()
        self.assertEqual(len(found), 1, found)
        self.assertIn("not committed", found[0])

    def test_a_repo_with_no_commit_gets_the_one_git_repository_problem_not_a_run_line_one(self):
        self.init()
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "blind.toml").write_text(
            (self.repo / "blind.toml").read_text().replace("exec ", ""))
        subprocess.run(["git", "init", "-q", str(plain)], check=True)
        cfg = {**self.loaded(), "blind_tests": {**BLOCK, "repo": str(plain)}}
        found = self.preflight_problems(cfg)
        self.assertEqual(len(found), 2, found)
        self.assertIn("git repository", found[0])
        self.assertIn("run line", found[1])        # the working copy; HEAD has nothing to judge
        self.assertNotIn("HEAD", found[1])

    def test_a_repo_without_a_commit_or_inside_another_repo_is_refused(self):
        self.init()
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "blind.toml").write_text((self.repo / "blind.toml").read_text())
        cfg = {**self.loaded(), "blind_tests": {**BLOCK, "repo": str(plain)}}
        self.assertTrue(any("git repository" in p for p in self.preflight_problems(cfg)))
        subprocess.run(["git", "init", "-q", str(plain)], check=True)       # a repo, no commit
        self.assertTrue(any("git repository" in p for p in self.preflight_problems(cfg)))

    def test_repo_inside_or_around_a_mount_is_refused(self):
        self.init()
        for mount in (self.repo, self.repo / "cards", self.tmp, "/"):
            with self.subTest(mount=mount):
                found = self.preflight_problems(mounts=[mount])
                self.assertTrue(any("overlaps" in p for p in found))
        self.assertEqual(self.preflight_problems(mounts=[self.tmp / "work"]), [])

    def test_a_symlink_into_a_mount_is_refused(self):
        self.init()
        work = self.tmp / "work"
        work.mkdir()
        link = self.tmp / "link"
        link.symlink_to(work)
        cfg = {**self.loaded(), "blind_tests": {**BLOCK, "repo": str(link)}}
        self.assertTrue(any("overlaps" in p for p in self.preflight_problems(cfg, [work])))

    def test_a_non_absolute_repo_is_refused(self):
        found = preflight.block_problems({**BLOCK, "repo": "blind/repo"})
        self.assertTrue(any("absolute" in p for p in found))

    def test_worker_mounts_are_the_board_dir_the_deps_mount_and_a_local_project_repo(self):
        deps = Path.home() / ".local" / "share" / "capsule-corp" / "deps"
        local = preflight.worker_mounts({"repo_url": "/srv/project"}, "b", self.home)
        self.assertEqual(local, [paths.board_dir("b", self.home), deps, Path("/srv/project")])
        as_url = preflight.worker_mounts({"repo_url": "file:///srv/p%20q"}, "b", self.home)
        self.assertEqual(as_url[2], Path("/srv/p q"))
        for remote in ("https://github.com/a/b.git", "git@github.com:a/b.git", "ssh://h/p", ""):
            with self.subTest(remote=remote):
                mounts = preflight.worker_mounts({"repo_url": remote}, "b", self.home)
                self.assertEqual(len(mounts), 2)

    def test_init_refuses_a_repo_inside_the_projects_local_checkout(self):
        cfg = {**json.loads(EXAMPLE_TEXT), "repo_url": str(self.tmp / "project")}
        self.cfg_path.write_text(json.dumps(cfg))
        self.refuses("mounts", repo=self.tmp / "project" / "blind")
        self.assertFalse((self.tmp / "project").exists())


class Cli(BlindTestCase):
    def test_init_then_preflight_through_the_real_parser(self):
        code, out, _ = self.init()
        self.assertEqual(code, 0, out)
        code, out, _ = run_cli("blind", "preflight", self.board, "--capsule-home", str(self.home))
        self.assertEqual((code, out.strip()), (0, "blind preflight ok"))

    def test_preflight_without_a_toml_says_unavailable_and_exits_1(self):
        self.init()
        (self.repo / "blind.toml").unlink()
        code, _, err = run_cli("blind", "preflight", self.board, "--capsule-home", str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("blind: unavailable", err)


class IntentRows(BlindTestCase):
    """Handlers for the intent rows this side owns (built-in expectations); the loader test waits
    for the CoS-written contracts/intent/blind_harness.jsonl."""

    def test_tester_glob_reaching_production_or_history_is_refused_and_blind_is_unavailable(self):
        for glob in ("././capsule/tick.py", ".git/**", "Capsule/**", "{capsule,docs}/**"):
            with self.subTest(glob=glob):
                cfg = json.loads(EXAMPLE_TEXT)
                cfg["blind_tests"] = {**BLOCK, "repo": str(self.repo), "tester_may_read": [glob]}
                self.cfg_path.write_text(json.dumps(cfg))
                code, _, err = run_cli("blind", "preflight", self.board,
                                       "--capsule-home", str(self.home))
                self.assertEqual(code, 1)
                self.assertIn("blind: unavailable", err)
                self.assertIn(glob, err)

    def test_loader_finds_this_sides_rows_once_the_file_exists(self):
        if not INTENT_FILE.exists():
            self.skipTest("contracts/intent/blind_harness.jsonl not written yet")
        ids = {json.loads(x)["id"] for x in INTENT_FILE.read_text().splitlines() if x.strip()}
        self.assertLessEqual(INTENT_IDS, ids)


class ContractTimeout(unittest.TestCase):
    def test_the_blind_run_and_the_lander_wait_at_least_as_long_as_a_contract_command(self):
        from capsule import lander, tick
        from capsule.blind import run as blind_run
        self.assertEqual(tick.CONTRACT_TIMEOUT, blind_run.BLIND_TIMEOUT)
        self.assertGreaterEqual(lander.TIMEOUT, tick.CONTRACT_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
