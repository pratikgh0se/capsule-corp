# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Template.test_deploy_sh_never_sees_it
"""`capsule board tick <name>`: the tick job for a new board without deploy.sh.

Every case runs in a fake world: a temp HOME, a temp CAPSULE_HOME, a local bare repo as the
checkout's `origin`, and a PATH whose `launchctl` fails and leaves a marker if it is ever called.
Two oracles per case: the printed lines with the exit code, and a direct read of the world
afterwards (a snapshot of the whole temp tree, the cloned repo's HEAD, the plist parsed back)."""
import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from capsule import board_tick, cli_ops, intent, paths, plists, preflight
from capsule.tests.public_tree import PRIVATE_TREE
from capsule.runners import docker

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgSign=false"]
LABEL = "dev.capsule.capsule-tick-demo"
VALUES = {"__PREFIX__": "dev.stub", "__HOME__": "/tmp/h", "__REPO__": "/tmp/r",
          "__CAPSULE_HOME__": "/tmp/h/cap"}


def git(*args, cwd=None):
    done = subprocess.run([*GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class _World(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.user, self.home = self.tmp / "user", self.tmp / "cap"
        self.bin, self.marker = self.tmp / "bin", self.tmp / "launchctl-called"
        self.user.mkdir()
        self.bin.mkdir()
        stub = self.bin / "launchctl"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{self.marker}"\nexit 1\n')
        stub.chmod(0o755)
        self.origin = self.tmp / "origin.git"
        self.checkout = self.tmp / "checkout"
        git("init", "-q", "--bare", "-b", "main", str(self.origin))
        git("clone", "-q", str(self.origin), str(self.checkout))
        self.commit("one")
        git("push", "-q", "origin", "HEAD:main", cwd=self.checkout)
        (self.home / "boards" / "demo").mkdir(parents=True)
        (self.home / "boards" / "demo" / "config.json").write_text('{"board": "demo"}\n')
        self.harness = self.home / "harness" / "main"
        self.plist = self.user / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def commit(self, name):
        (self.checkout / name).write_text(name)
        git("add", name, cwd=self.checkout)
        git("commit", "-q", "-m", name, cwd=self.checkout)

    def snapshot(self):
        """{relative path: (kind, sha1 or link target)} for everything under the temp folder,
        except the origin and the checkout (a clone reads them, the test advances them)."""
        out = {}
        for p in sorted(self.tmp.rglob("*")):
            rel = p.relative_to(self.tmp)
            if rel.parts[0] in ("origin.git", "checkout"):
                continue
            if p.is_symlink():
                out[str(rel)] = ("link", os.readlink(p))
            elif p.is_file():
                out[str(rel)] = ("file", hashlib.sha1(p.read_bytes()).hexdigest())
            else:
                out[str(rel)] = ("dir",)
        return out

    @staticmethod
    def changed(before, after):
        """The paths that differ between two snapshots: added, removed or rewritten."""
        return {p for p, _ in set(before.items()) ^ set(after.items())}

    def tick(self, *flags, name="demo", home_env=True, extra_env=None, capsule_home=None):
        """(exit code, stdout lines, stderr text) of `capsule board tick <name> <flags>`.
        `home_env=False` leaves CAPSULE_HOME unset (the default home is then under HOME)."""
        args = argparse.Namespace(rest=[name], dry_run="--dry-run" in flags,
                                  yes="--yes" in flags, capsule_home=capsule_home)
        world = {"HOME": str(self.user), "PATH": f"{self.bin}:/usr/bin:/bin",
                 **({"CAPSULE_HOME": str(self.home)} if home_env else {}), **(extra_env or {})}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, world))
            for gone in ("CAPSULE_LAUNCHD_PREFIX", "AGENT_OPS_NO_SEND") + (
                    () if home_env else ("CAPSULE_HOME",)):
                if gone not in (extra_env or {}):
                    os.environ.pop(gone, None)
            stack.enter_context(mock.patch.object(paths, "REPO", self.checkout))
            stack.enter_context(mock.patch("builtins.input", side_effect=EOFError))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = board_tick.run(args, mock.Mock())
        return rc, out.getvalue().splitlines(), err.getvalue()

    @staticmethod
    def statuses(lines):
        return {ln.split(" ", 1)[0] for ln in lines}


class Run(_World):
    def test_dry_run_writes_nothing_and_lists_the_steps(self):
        before = self.snapshot()
        rc, lines, _ = self.tick("--dry-run")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(self.snapshot(), before)                                  # oracle 2
        text = "\n".join(lines)
        for want in ("WOULD create", f"WOULD git clone {self.origin}", "WOULD write",
                     f"{LABEL}.plist", docker.DEPS_MOUNT):
            self.assertIn(want, text)
        self.assertLessEqual(self.statuses(lines), {"WOULD", "ok", "next"}, lines)
        self.assertFalse(self.marker.exists())

    def test_a_real_run_clones_the_harness_and_writes_the_plist(self):
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.harness),                # oracle 2
                         git("rev-parse", "HEAD", cwd=self.checkout))
        self.assertEqual(git("remote", "get-url", "origin", cwd=self.harness), str(self.origin))
        self.assertTrue((self.home / "logs").is_dir())
        doc = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(doc["Label"], LABEL)
        self.assertEqual(doc["WorkingDirectory"], str(self.harness))
        self.assertEqual(doc["EnvironmentVariables"]["CAPSULE_HOME"], str(self.home))
        self.assertEqual(doc["ProgramArguments"][0], sys.executable)    # the one running it
        self.assertTrue((self.user / docker.DEPS_MOUNT).is_dir())        # the folder `check` wants
        self.assertEqual(list((self.user / docker.DEPS_MOUNT).iterdir()), [])        # made empty
        self.assertEqual(sorted(p.name for p in self.plist.parent.iterdir()), [self.plist.name])
        script = shlex.quote(str(paths.REPO / "bin" / "capsule-tick.sh"))     # absolute path
        self.assertEqual(lines[-1], f"next start it: CAPSULE_HOME={shlex.quote(str(self.home))} "
                         f"{script} start demo")                             # home from the env
        self.assertNotRegex("\n".join(lines), r"\bP\d\d")                          # no item codes
        self.assertFalse(self.marker.exists())                                     # no launchctl

    def test_a_second_run_changes_nothing_and_never_pulls(self):
        self.tick("--yes")
        head = git("rev-parse", "HEAD", cwd=self.harness)
        self.commit("two")
        git("push", "-q", "origin", "HEAD:main", cwd=self.checkout)               # origin moves on
        before = self.snapshot()
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 0, lines)
        self.assertLessEqual(self.statuses(lines), {"ok", "skip", "next"}, lines)
        self.assertEqual(lines[-1].split(" ", 1)[0], "next")
        self.assertEqual(self.snapshot(), before)                                  # oracle 2
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.harness), head)
        self.assertFalse(self.marker.exists())

    def test_a_differing_plist_is_left_alone_and_no_start_line_is_printed(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("mine\n")
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(self.plist.read_text(), "mine\n")
        self.assertIn(f"skip {self.plist} differs: move it, then rerun", lines)
        self.assertNotIn("next", self.statuses(lines))

    def test_a_symlinked_plist_is_left_alone(self):
        self.tick("--yes")                       # the file a run would write ...
        target = self.tmp / "elsewhere.plist"
        text = self.plist.read_text()
        self.plist.rename(target)
        self.plist.symlink_to(target)            # ... now sits behind a symlink
        _, lines, _ = self.tick("--yes")
        self.assertTrue(self.plist.is_symlink())
        self.assertEqual(os.readlink(self.plist), str(target))
        self.assertEqual(target.read_text(), text)
        self.assertIn(f"skip {self.plist} differs: move it, then rerun", lines)
        self.assertNotIn("next", self.statuses(lines))

    def test_a_folder_that_is_not_a_git_checkout_is_refused_and_untouched(self):
        self.harness.mkdir(parents=True)
        (self.harness / "mine.txt").write_text("keep\n")
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 1)
        self.assertTrue([ln for ln in lines
                         if ln.startswith("FAILED") and "not a git checkout" in ln])
        self.assertEqual(sorted(p.name for p in self.harness.iterdir()), ["mine.txt"])
        self.assertFalse(self.plist.exists())
        self.assertNotIn("next", self.statuses(lines))

    def test_a_board_without_a_config_is_refused_and_nothing_is_written(self):
        (self.home / "boards" / "demo" / "config.json").unlink()
        before = self.snapshot()
        rc, lines, err = self.tick("--yes")
        self.assertEqual((rc, lines), (1, []))
        self.assertIn("refused: no board 'demo'", err)
        self.assertEqual(self.snapshot(), before)

    def test_a_bad_board_name_is_refused(self):
        before = self.snapshot()
        for name in ("../demo", "tick", "a/b"):
            with self.subTest(name):
                rc, _, err = self.tick("--yes", name=name)
                self.assertEqual(rc, 1)
                self.assertIn("the board name must be", err)
        self.assertEqual(self.snapshot(), before)

    def test_a_checkout_with_no_origin_is_cloned_from_its_own_folder(self):
        git("remote", "remove", "origin", cwd=self.checkout)
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(git("remote", "get-url", "origin", cwd=self.harness), str(self.checkout))
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.harness),
                         git("rev-parse", "HEAD", cwd=self.checkout))

    def test_a_failed_clone_is_reported_and_exits_1(self):
        git("remote", "set-url", "origin", str(self.tmp / "nope.git"), cwd=self.checkout)
        rc, lines, _ = self.tick("--yes")
        self.assertEqual(rc, 1)
        self.assertTrue([ln for ln in lines if ln.startswith("FAILED git clone")], lines)
        self.assertFalse(self.plist.exists())

    def test_the_label_prefix_comes_from_host_json_and_a_no_answer_changes_nothing(self):
        (self.home / "host.json").write_text('{"launchd": {"label_prefix": "com.mine"}}')
        before = self.snapshot()
        _, lines, _ = self.tick()                                      # no --yes, no answers
        self.assertEqual(self.snapshot(), before)
        self.assertTrue([ln for ln in lines if ln.endswith("(declined)")])
        self.tick("--yes")
        self.assertTrue((self.user / "Library/LaunchAgents/com.mine.capsule-tick-demo.plist")
                        .is_file())

    def test_the_command_is_wired_into_the_cli(self):
        env = {**os.environ, "HOME": str(self.user), "CAPSULE_HOME": str(self.home),
               "GIT_ALLOW_PROTOCOL": "file"}         # a broken dry run can never clone off-box
        done = subprocess.run([sys.executable, "-m", "capsule", "board", "tick", "demo",
                               "--dry-run"], cwd=paths.REPO, capture_output=True, text=True,
                              timeout=60, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"WOULD write {self.plist}", done.stdout)


class Template(unittest.TestCase):
    TEMPLATE = board_tick.TEMPLATE
    OLD = paths.REPO / "launchd" / "templates" / "__PREFIX__.capsule-tick-capsule-corp.plist"

    def rendered(self, board="demo"):
        return plists.render(self.TEMPLATE.read_text(),
                             {**VALUES, plists.BOARD: board, plists.PYTHON: "/opt/py/python3.11"})

    def test_the_rendered_plist_is_valid_and_runs_the_board_from_the_harness(self):
        text = self.rendered()
        self.assertNotRegex(text, r"__[A-Z_]+__")
        ET.fromstring(text)
        doc = plistlib.loads(text.encode())
        args = doc["ProgramArguments"]
        self.assertEqual(args[args.index("--board"):], ["--board", "demo"])
        self.assertEqual(args[:2], ["/opt/py/python3.11", "-u"])     # pinned, never a bare python3
        self.assertNotIn("python3", args)
        self.assertEqual(doc["WorkingDirectory"], "/tmp/h/cap/harness/main")
        self.assertEqual(doc["Label"], "dev.stub.capsule-tick-demo")
        self.assertEqual(doc["StandardOutPath"], "/tmp/h/cap/logs/tick-demo.log")
        self.assertEqual(doc["StandardErrorPath"], doc["StandardOutPath"])
        env = doc["EnvironmentVariables"]
        self.assertEqual(env["CAPSULE_HOME"], "/tmp/h/cap")
        self.assertNotIn("EVENTS_SPOOL", env)       # the tick and `capsule status` share a default
        self.assertNotIn("OBSIDIAN_VAULT_PATH", env)
        self.assertNotIn("obsidian", text.lower())

    def test_plutil_accepts_it(self):
        if not shutil.which("plutil"):
            self.skipTest("plutil is macOS-only")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.plist"
            path.write_text(self.rendered())
            done = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_it_keeps_the_capsule_corp_templates_keys_and_values(self):
        old = plistlib.loads(self.OLD.read_text().replace("__", "_").encode())
        new = plistlib.loads(self.rendered("capsule-corp").encode())
        self.assertEqual(set(new), set(old))
        for key in ("RunAtLoad", "KeepAlive", "ThrottleInterval", "ExitTimeOut"):
            self.assertEqual(new[key], old[key], key)
        old_args = old["ProgramArguments"]
        self.assertEqual(old_args[:2], ["/usr/bin/env", "python3"])    # deploy.sh keeps PATH's
        self.assertEqual(new["ProgramArguments"][1:], old_args[2:])    # only the interpreter
        self.assertEqual(set(new["EnvironmentVariables"]),
                         set(old["EnvironmentVariables"])
                         - {"OBSIDIAN_VAULT_PATH", "EVENTS_SPOOL"} | {"CAPSULE_HOME"})

    def test_deploy_sh_never_sees_it(self):
        self.assertEqual(self.TEMPLATE.parent.name, "board")
        self.assertFalse(list((paths.REPO / "launchd" / "templates").glob("*__BOARD__*")))
        if PRIVATE_TREE:
            deploy = (paths.REPO / "deploy.sh").read_text()
            self.assertIn('"$REPO"/launchd/templates/*.plist', deploy)


class Placeholders(unittest.TestCase):
    TEXT = "<string>__BOARD__ __PREFIX__</string>"

    def test_the_renderer_does_not_depend_on_cli_ops(self):
        tree = ast.parse(Path(plists.__file__).read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                    for a in n.names}
        self.assertNotIn("cli_ops", imported)
        self.assertIs(plists.paths, paths)
        self.assertEqual(paths.board_name_problem("a.b"), None)
        self.assertIn("the board name must be", paths.board_name_problem("tick"))
        self.assertFalse(hasattr(cli_ops, "board_name_problem"))

    def test_board_is_filled_only_with_a_valid_name(self):
        self.assertEqual(plists.render(self.TEXT, {**VALUES, plists.BOARD: "a.b-c_1"}),
                         "<string>a.b-c_1 dev.stub</string>")
        for bad in ("../x", "new", "tick", "a b", "-x", "a<b"):
            with self.subTest(bad):
                self.assertRaises(ValueError, plists.render, self.TEXT,
                                  {**VALUES, plists.BOARD: bad})

    def test_python_is_filled_only_when_given_and_escaped(self):
        text = "<string>__PYTHON__</string>"
        self.assertEqual(plists.render(text, {**VALUES, plists.PYTHON: "/a & b/python3.11"}),
                         "<string>/a &amp; b/python3.11</string>")
        self.assertRaises(ValueError, plists.render, text, VALUES)

    def test_board_without_a_value_and_any_other_name_stay_errors(self):
        self.assertRaises(ValueError, plists.render, self.TEXT, VALUES)
        self.assertRaises(ValueError, plists.render, "__VAULT__", {**VALUES, plists.BOARD: "x"})


WORLD = "temp HOME and CAPSULE_HOME, a board config, a local origin, stub launchctl"
CASES = {
    "dry-run-writes-nothing": (
        {"check": "board tick", "mode": "--dry-run",
         "world": "temp HOME and CAPSULE_HOME, a board config, a local origin, stub launchctl"},
        {"exit": 0, "files_changed": 0, "steps_listed": True, "launchctl_calls": 0}),
    "second-run-only-ok-skip-next": (
        {"check": "board tick", "mode": "--yes, run twice",
         "world": "temp HOME and CAPSULE_HOME, a board config, a local origin, stub launchctl"},
        {"first_exit": 0, "second_exit": 0, "second_run_changes": 0,
         "second_run_other_lines": 0, "second_run_has_next": True, "launchctl_calls": 0}),
    "differing-plist-left-alone": (
        {"check": "board tick", "mode": "--yes, a different plist already installed",
         "world": "temp HOME and CAPSULE_HOME, a board config, a local origin, stub launchctl"},
        {"exit": 0, "plist_changed": False, "skip_line_printed": True, "next_line_printed": False,
         "launchctl_calls": 0}),
    "plist-runs-the-python-that-ran-the-command": (
        {"check": "board tick", "mode": "--yes, read the plist back", "world": WORLD},
        {"exit": 0, "program_is_the_running_python": True, "no_bare_python3": True,
         "launchctl_calls": 0}),
    "python-3-9-gets-one-plain-line": (
        {"check": "python -m capsule", "mode": "interpreter reports 3.9, any command"},
        {"exit": 1, "stdout_empty": True, "stderr_lines": 1, "names_3_11": True,
         "names_install_sh": True, "names_the_launcher": True}),
    "check-serves-a-legacy-board": (
        {"check": "check", "mode": "--board my-app, no per-board config, "
                                   "legacy config.json names my-app"},
        {"config_level": "ok", "from_the_legacy_config": True}),
}


class IntentCases(_World):
    def calls(self):
        return len(self.marker.read_text().splitlines()) if self.marker.exists() else 0

    def observe_other(self, document):
        """The cases that do not run `board tick` in a plain fresh world."""
        mode = document["mode"]
        if mode == "--yes, read the plist back":
            rc, _, _ = self.tick("--yes")
            argv = plistlib.loads(self.plist.read_bytes())["ProgramArguments"]
            return {"exit": rc, "program_is_the_running_python": argv[0] == sys.executable,
                    "no_bare_python3": "python3" not in argv[:1] and "__PYTHON__" not in argv,
                    "launchctl_calls": self.calls()}
        if mode.startswith("interpreter reports 3.9"):
            code = ("import runpy, sys; sys.version_info = (3, 9, 6, 'final', 0); "
                    "runpy.run_module('capsule', run_name='__main__')")
            r = subprocess.run([sys.executable, "-c", code, "doctor"], cwd=paths.REPO,
                               capture_output=True, text=True, timeout=30)
            return {"exit": r.returncode, "stdout_empty": r.stdout == "",
                    "stderr_lines": len(r.stderr.strip().splitlines()),
                    "names_3_11": "3.11" in r.stderr,
                    "names_install_sh": "./install.sh" in r.stderr,
                    "names_the_launcher": "~/.local/bin/capsule" in r.stderr}
        return self.observe_check()

    def observe_check(self, board="my-app"):
        """`capsule check --board my-app` (preflight.run) with the legacy config.json naming
        my-app, and no docker, no claude: only the `config` row is read."""
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg.update(board="my-app", repo_url=str(self.tmp))
        (self.home / "config.json").write_text(json.dumps(cfg))
        stub = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
        rows = preflight.run(self.home, board=board, run_cmd=stub, which=lambda _name: None)
        (level, _, detail), = [r for r in rows if r[1] == "config"]
        legacy = str(self.home / "config.json")
        return {"config_level": level, "from_the_legacy_config": detail.startswith(legacy)}

    def observe(self, document):
        if document["mode"] not in ("--dry-run", "--yes, run twice",
                                    "--yes, a different plist already installed"):
            return self.observe_other(document)
        before = self.snapshot()
        if document["mode"] == "--dry-run":
            rc, lines, _ = self.tick("--dry-run")
            after = self.snapshot()
            return {"exit": rc, "launchctl_calls": self.calls(),
                    "files_changed": len(self.changed(before, after)),
                    "steps_listed": all(any(w in ln for ln in lines) for w in
                                        ("git clone", "write", LABEL))}
        if document["mode"] == "--yes, run twice":
            first, _, _ = self.tick("--yes")
            mid = self.snapshot()
            second, lines, _ = self.tick("--yes")
            end = self.snapshot()
            return {"first_exit": first, "second_exit": second,
                    "second_run_changes": len(self.changed(mid, end)),
                    "second_run_other_lines": len(
                        [ln for ln in lines if ln.split(" ", 1)[0] not in ("ok", "skip", "next")]),
                    "second_run_has_next": lines[-1].startswith("next "),
                    "launchctl_calls": self.calls()}
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("mine\n")
        rc, lines, _ = self.tick("--yes")
        return {"exit": rc, "plist_changed": self.plist.read_text() != "mine\n",
                "skip_line_printed": any(ln.startswith("skip ") and "differs" in ln
                                         for ln in lines),
                "next_line_printed": any(ln.startswith("next ") for ln in lines),
                "launchctl_calls": self.calls()}

    def assert_case(self, name, document, want):
        got = self.observe(document)
        self.assertTrue(intent.matches(want, got), f"{name}: wanted {want}, got {got}")

    def test_the_built_in_expectations(self):
        for name, (document, want) in CASES.items():
            with self.subTest(name):
                self.setUp()
                self.assert_case(name, document, want)

    def test_the_shipped_rows(self):
        if not intent.role_path("board_tick_setup").is_file():
            self.skipTest("contracts/intent/board_tick_setup.jsonl is not shipped yet")
        rows = intent.load("board_tick_setup")
        self.assertEqual({r["id"] for r in rows}, set(CASES))
        for row in rows:
            with self.subTest(row["id"]):
                self.setUp()
                self.assert_case(row["id"], row["given"]["document"], row["expect"])


class NextLines(unittest.TestCase):
    def test_board_new_names_board_tick_before_the_start_line(self):
        self.assertEqual(cli_ops.NEXT[1], "~/.local/bin/capsule board tick {0}{1}")
        self.assertEqual(cli_ops.NEXT[2], cli_ops.START)


if __name__ == "__main__":
    unittest.main()
