"""P07 `capsule board new <name> --repo <url>`: writes only a new boards/<name>/config.json under a
temp CAPSULE_HOME. Oracles: the written config (loaded by `config.load` and read raw) and the
refusal (exit code and message, the file untouched)."""
import contextlib
import importlib
import io
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from capsule import cli_ops, config, paths, workspace


def board_new(home, *argv):
    env = dict(os.environ)
    return subprocess.run([sys.executable, "-m", "capsule", "board", "new", *argv,
                           "--capsule-home", str(home)], cwd=paths.REPO, capture_output=True,
                          text=True, timeout=60, env=env)


class BoardNew(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)

    def cfg_path(self, name="demo"):
        return self.home / "boards" / name / "config.json"

    def test_writes_a_valid_config_and_prints_the_next_four_commands(self):
        repo = "https://github.com/you/acme-app.git"
        r = board_new(self.home, "acme", "--repo", repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        path = self.cfg_path("acme")
        cfg = config.load(path)                                  # oracle 1: it loads and validates
        raw = json.loads(path.read_text())                       # oracle 2: the file itself
        self.assertEqual((raw["board"], raw["repo_url"], cfg["board"]), ("acme", repo, "acme"))
        self.assertEqual(raw["epics"], ["epic/acme"])
        for ref in raw["epics"]:
            workspace._validate_ref(ref)                         # dispatch would accept it
        example = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        self.assertNotIn(example["board"], json.dumps(raw).replace(repo, ""))
        self.assertNotIn("unblocked", raw)
        self.assertEqual(raw["caps"], example["caps"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        steps = r.stdout.splitlines()[-4:]                       # the steps come last
        for step, want in zip(steps, ("capsule check --board acme", "capsule board tick acme",
                                      "capsule-tick.sh start acme",
                                      "capsule intake acme --title")):
            self.assertIn(want, step)
            self.assertNotIn("python3 -m", step)                 # stock python3 is 3.9
            self.assertIn(f"{self.home}", step)                  # a non-default home is kept
        self.assertEqual(sorted(p.name for p in self.home.rglob("*") if p.is_file()),
                         ["config.json", "kanban.db"])                    # nothing else written
        db = self.home / "boards" / "acme" / "kanban.db"
        self.assertTrue(db.read_bytes().startswith(b"SQLite format 3\x00"))
        con = sqlite3.connect(db)
        try:
            self.assertIsNotNone(con.execute(
                "select 1 from sqlite_master where type='table' and name='tasks'").fetchone())
        finally:
            con.close()

    def test_a_new_board_runs_the_image_the_tester_built(self):
        r = board_new(self.home, "acme", "--repo", "https://github.com/you/acme-app.git")
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = json.loads(self.cfg_path("acme").read_text())
        self.assertEqual(raw["redesign"].get("image_loop", "record"), "record")
        loaded = config.load(self.cfg_path("acme"))
        self.assertEqual(config.redesign(loaded)["image_loop"], "record")
        example = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        self.assertEqual({k: v for k, v in raw["redesign"].items() if k != "image_loop"},
                         example["redesign"])

    def test_every_url_form_and_an_absolute_local_path_are_accepted(self):
        local = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, local, True)
        for i, repo in enumerate(("https://github.com/you/r", "ssh://git@host:2222/you/r.git",
                                  "git@github.com:you/r.git", str(local))):
            with self.subTest(repo=repo):
                r = board_new(self.home, f"b{i}", "--repo", repo)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(self.cfg_path(f"b{i}").read_text())["repo_url"], repo)

    def test_existing_board_is_refused_and_left_untouched(self):
        board_new(self.home, "demo", "--repo", "https://github.com/you/one.git")
        before = self.cfg_path().read_bytes()
        r = board_new(self.home, "demo", "--repo", "https://github.com/you/two.git")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused: board 'demo' already exists", r.stderr)
        self.assertEqual(self.cfg_path().read_bytes(), before)               # oracle 2
        (self.home / "boards" / "db-only").mkdir()                    # a dir alone is taken too
        self.assertEqual(board_new(self.home, "db-only", "--repo",
                                   "https://github.com/you/r").returncode, 1)

    def test_refusals_write_nothing(self):
        inside = self.home / "boards" / "x"
        cases = {
            ("demo", "--repo", "relative/path"): "--repo must be",
            ("demo", "--repo", "./repo"): "--repo must be",
            ("demo", "--repo", "http://github.com/you/r"): "--repo must be",
            ("demo", "--repo", "https://tok@github.com/you/r"): "--repo must be",
            ("demo", "--repo=-oProxyCommand=x"): "unsafe repo url",
            ("demo", "--repo", "ext::sh -c x"): "disallowed git transport",
            ("demo", "--repo", str(inside)): "overlaps CAPSULE_HOME",
            ("demo", "--repo", str(self.home.parent)): "overlaps CAPSULE_HOME",
            ("demo", "--repo", str(paths.REPO)): "overlaps the Capsule checkout",
            ("demo", "--repo", str(paths.REPO / "capsule")): "overlaps the Capsule checkout",
            ("new", "--repo", "https://github.com/you/r"): "not `new`",
            ("tick", "--repo", "https://github.com/you/r"): "or `tick`",
            ("../evil", "--repo", "https://github.com/you/r"): "the board name must be",
            ("a/b", "--repo", "https://github.com/you/r"): "the board name must be",
        }
        for argv, why in cases.items():
            with self.subTest(argv=argv):
                r = board_new(self.home, *argv)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(why, r.stderr)
                self.assertNotIn("tok@", r.stderr)
        self.assertFalse((self.home / "boards").exists())

    def new_with_env(self, name, **env):
        """`board new <name>` with HOME at a temp folder and the given CAPSULE_HOME (None =
        unset, so the home is the default one under that HOME); the `next:` part of stdout."""
        full = {k: v for k, v in os.environ.items()
                if k not in ("CAPSULE_HOME", "AGENT_OPS_NO_SEND")}
        full.update({k: v for k, v in env.items() if v is not None}, HOME=str(self.home))
        r = subprocess.run([sys.executable, "-m", "capsule", "board", "new", name, "--repo",
                            "https://github.com/you/r"], cwd=paths.REPO, capture_output=True,
                           text=True, timeout=60, env=full)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split("next:")[1]

    def test_a_default_home_board_prints_plain_next_commands(self):
        steps = self.new_with_env("plain")                      # HOME=temp, CAPSULE_HOME unset
        self.assertTrue((self.home / ".local/share/capsule/boards/plain/config.json").is_file())
        self.assertNotIn("capsule-home", steps)
        self.assertNotIn("CAPSULE_HOME", steps)

    def test_a_home_named_by_the_environment_is_kept_in_the_next_commands(self):
        elsewhere = self.home / "elsewhere"
        steps = self.new_with_env("env", CAPSULE_HOME=str(elsewhere))
        tick = shlex.quote(str(paths.REPO / "bin" / "capsule-tick.sh"))
        self.assertIn(f"CAPSULE_HOME={elsewhere} {tick} start env", steps)
        self.assertIn(f"board tick env --capsule-home {elsewhere}", steps)

    def test_a_dotdot_name_is_refused_and_every_epic_ref_is_checked(self):
        r = board_new(self.home, "a..b", "--repo", "https://github.com/you/r")
        self.assertEqual(r.returncode, 1)
        self.assertIn("the board name must be", r.stderr)
        r = board_new(self.home, "x.lock", "--repo", "https://github.com/you/r")   # a bad ref
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("unsafe epic branch", r.stderr)
        self.assertRaises(ValueError, workspace._validate_ref, "epic/x.lock")      # oracle 2
        self.assertFalse((self.home / "boards").exists())

    def test_an_ssh_host_cannot_start_with_a_dash(self):
        for repo in ("ssh://-oProxyCommand/x", "ssh://git@-oProxyCommand:22/x",
                     "git@-oProxyCommand:x"):
            with self.subTest(repo=repo):
                r = board_new(self.home, "demo", "--repo", repo)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("--repo must be", r.stderr)
                self.assertIsNone(cli_ops.REMOTE.fullmatch(repo))             # oracle 2
        self.assertTrue(cli_ops.REMOTE.fullmatch("ssh://git@host:22/x"))

    def test_a_local_repo_must_exist_and_have_no_control_character(self):
        gone = self.home.parent / f"{self.home.name}-gone"
        r = board_new(self.home, "demo", "--repo", str(gone))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--repo path does not exist", r.stderr)
        self.assertFalse(gone.exists())                                      # oracle 2
        r = board_new(self.home, "demo", "--repo", "/tmp/one\ntwo")
        self.assertEqual(r.returncode, 1)
        self.assertIn("control characters", r.stderr)

    def upper(self, path):
        """`path` upper-cased, or skip when this disk is case-sensitive (the name differs)."""
        loud = str(path).upper()
        if not os.path.exists(loud) or not os.path.samefile(loud, path):
            self.skipTest("case-sensitive filesystem")
        return loud

    def test_an_upper_cased_home_or_checkout_path_still_overlaps(self):
        (self.home / "boards").mkdir()
        for loud, label in ((self.upper(self.home / "boards"), "CAPSULE_HOME"),
                            (self.upper(self.home), "CAPSULE_HOME"),
                            (self.upper(paths.REPO), "the Capsule checkout"),
                            (self.upper(paths.REPO / "capsule"), "the Capsule checkout")):
            with self.subTest(repo=loud):
                r = board_new(self.home, "demo", "--repo", loud)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(f"overlaps {label}", r.stderr)
        self.assertFalse(self.cfg_path().exists())                          # oracle 2

    def test_two_boards_claiming_one_name_at_once_leave_the_first_one(self):
        """The other process wins the race inside the window between the checks and the claim."""
        real = cli_ops._new_config

        def other_wins(name, repo, checks, notify):
            self.cfg_path(name).parent.mkdir(parents=True)
            self.cfg_path(name).write_text("first\n")
            return real(name, repo, checks, notify)

        args = SimpleNamespace(rest=["demo"], repo="https://github.com/you/r", check=None,
                               notify=False, capsule_home=str(self.home))
        err = io.StringIO()
        with mock.patch.object(cli_ops, "_new_config", other_wins), \
                contextlib.redirect_stderr(err):
            rc = cli_ops._board_new(args, None, self.home)
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err.getvalue())
        self.assertEqual(self.cfg_path().read_text(), "first\n")             # oracle 2

    def test_usage_errors_exit_2(self):
        self.assertEqual(board_new(self.home, "demo").returncode, 2)
        self.assertEqual(board_new(self.home, "--repo", "https://github.com/you/r").returncode, 2)

    REPO = "https://github.com/you/acme-app.git"

    def test_check_flags_replace_the_contract_commands_in_order(self):
        r = board_new(self.home, "acme", "--repo", self.REPO, "--check", "npm test",
                      "--check", "make lint")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.cfg_path("acme").read_text())["contract_commands"],
                         ["npm test", "make lint"])
        self.assertEqual(config.load(self.cfg_path("acme"))["contract_commands"],
                         ["npm test", "make lint"])
        self.assertNotIn("note: default", r.stdout)

    def test_no_check_keeps_the_defaults_and_names_them_in_a_note(self):
        r = board_new(self.home, "acme", "--repo", self.REPO)
        self.assertEqual(r.returncode, 0, r.stderr)
        want = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cmds = json.loads(self.cfg_path("acme").read_text())["contract_commands"]
        self.assertEqual(cmds, want["contract_commands"])
        lines = r.stdout.splitlines()
        notes = [i for i, x in enumerate(lines) if x.startswith("note: default")]
        self.assertEqual(len(notes), 1)
        start = notes[0]
        self.assertIn("--check", lines[start])
        self.assertEqual(lines[start + 1:start + 1 + len(cmds)], ["  " + c.strip() for c in cmds])
        self.assertLess(start, lines.index("next:"))        # the note comes before the steps

    def test_an_empty_or_blank_check_is_refused_and_writes_nothing(self):
        for bad in ("", "   "):
            for argv in (("--check", bad), ("--check", "npm test", "--check", bad)):
                r = board_new(self.home, "acme", "--repo", self.REPO, *argv)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("--check", r.stderr)
                self.assertFalse((self.home / "boards" / "acme").exists())

    def test_every_tick_script_next_line_is_absolute_and_exists(self):
        r = board_new(self.home, "acme", "--repo", self.REPO)
        lines = [x for x in r.stdout.splitlines() if "capsule-tick.sh" in x]
        self.assertEqual(len(lines), 1)
        script = shlex.split(lines[0])[-3]
        self.assertTrue(os.path.isabs(script), script)
        self.assertTrue(os.path.isfile(script), script)

    def test_a_brace_in_the_checkout_path_does_not_break_the_start_line(self):
        repo = self.home / "{x}"
        self.addCleanup(importlib.reload, cli_ops)
        with mock.patch.object(paths, "REPO", repo):
            importlib.reload(cli_ops)
            line = cli_ops.START.format("demo", "", "")
        self.assertEqual(line, shlex.quote(str(repo / "bin" / "capsule-tick.sh")) + " start demo")

    def test_board_help_shows_the_new_usage_and_a_check_example(self):
        r = subprocess.run([sys.executable, "-m", "capsule", "board", "--help"], cwd=paths.REPO,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("board new <name> --repo", r.stdout)
        self.assertRegex(r.stdout, r"example:.*--check")

    def test_check_with_another_board_form_exits_2_naming_it(self):
        for argv in (("nope",), ("tick", "nope"), ()):
            r = subprocess.run([sys.executable, "-m", "capsule", "board", *argv, "--check", "x",
                                "--capsule-home", str(self.home)], cwd=paths.REPO,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, argv)
            self.assertIn("--check", r.stderr)

    def slack(self, name, *extra):
        env = dict(os.environ, AGENT_OPS_NO_SEND="1")
        r = subprocess.run([sys.executable, "-m", "capsule", "board", "new", name, "--repo",
                            self.REPO, *extra, "--capsule-home", str(self.home)], cwd=paths.REPO,
                           capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        raw = json.loads(self.cfg_path(name).read_text())
        config.load(self.cfg_path(name))                          # statement 4: it validates
        return r, raw["slack"]

    def test_the_channel_is_lowercase_with_dots_turned_to_dashes(self):
        r, got = self.slack("My.App", "--notify")
        self.assertEqual(got["channels"]["done"], "#cc-my-app")
        self.assertEqual(got["channels"]["scheduled"], "#cc-my-app")
        self.assertTrue(self.cfg_path("My.App").exists())          # the board name is unchanged
        self.assertIn("channel #cc-my-app must exist", r.stdout)

    def test_the_notify_note_says_where_messages_go_in_at_most_100_columns(self):
        r, _ = self.slack("acme", "--notify")
        notes = [x for x in r.stdout.splitlines()
                 if x.startswith("note:") and "default contract" not in x]
        text = " ".join(notes)
        for chan in ("#capsule-needs-you", "#capsule-alerts", "#capsule-activity"):
            self.assertIn(chan, text)
        self.assertTrue(all(len(x) <= 100 for x in notes), notes)

    def test_notify_turns_slack_on_and_renames_only_the_example_board_channels(self):
        want = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())["slack"]
        r, got = self.slack("acme", "--notify")
        self.assertIs(got["enabled"], True)
        self.assertEqual(got["identity"], want["identity"])
        self.assertEqual(got["channels"], {k: v.replace("#cc-my-app", "#cc-acme")
                                           for k, v in want["channels"].items()})
        self.assertEqual(got["channels"]["done"], "#cc-acme")
        self.assertEqual(got["channels"]["blocked"], "#capsule-needs-you")
        self.assertEqual(got["channels"]["incident"], "#capsule-alerts")
        self.assertEqual(got["channels"]["running"], "#capsule-activity")
        notes = [x for x in r.stdout.splitlines() if x.startswith("note:") and "#cc-acme" in x]
        self.assertEqual(len(notes), 1)
        self.assertIn("invited", notes[0])
        self.assertEqual(sorted(p.name for p in self.home.rglob("*") if p.is_file()),
                         ["config.json", "kanban.db"])          # statement 6: no outbox row

    def test_without_notify_slack_stays_off_and_a_note_names_the_flag(self):
        want = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())["slack"]
        r, got = self.slack("acme")
        self.assertIs(got["enabled"], False)
        self.assertEqual(got["channels"], {k: v.replace("#cc-my-app", "#cc-acme")
                                           for k, v in want["channels"].items()})
        notes = [x for x in r.stdout.splitlines() if x.startswith("note:") and "--notify" in x]
        self.assertEqual(len(notes), 1)
        self.assertIn("turns it on", notes[0])

    def test_notify_with_another_board_form_exits_2_naming_it(self):
        for argv in (("nope",), ("tick", "nope"), ()):
            r = subprocess.run([sys.executable, "-m", "capsule", "board", *argv, "--notify",
                                "--capsule-home", str(self.home)], cwd=paths.REPO,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, argv)
            self.assertIn("--notify", r.stderr)

    def test_plain_board_browsing_is_unchanged(self):
        r = subprocess.run([sys.executable, "-m", "capsule", "board", "nope", "extra",
                            "--capsule-home", str(self.home)], cwd=paths.REPO,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("board takes one board name", r.stderr)


if __name__ == "__main__":
    unittest.main()
