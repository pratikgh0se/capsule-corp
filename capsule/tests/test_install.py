"""P08 `capsule install`, plus the Z1 handlers for its three intent rows.

Every case runs in a fake world (reusing test_doctor's `_World`): a temp HOME holding CAPSULE_HOME,
a PATH of stub tools that log each call (brew, docker, launchctl, claude, gh) and a patched disk
size. Two oracles per case: the installer's printed lines and exit code, and a direct read of the
world afterwards (a snapshot of the temp HOME tree, the stub call log, the written files read raw).
The real brew, launchctl and docker are never reached: PATH holds only the stubs."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "maintenance"))
import docker_engine  # noqa: E402

from capsule import doctor, install, intent, paths  # noqa: E402
from capsule.runners import docker
from capsule.tests.test_doctor import _World

BREW = ('echo "brew $*" >> "$STUB_LOG"\n'
        'echo "$HOMEBREW_NO_AUTO_UPDATE" >> "$STUB_LOG.noauto"\n'
        'echo "$HOMEBREW_BUNDLE_NO_UPGRADE" >> "$STUB_LOG.noupgrade"\n'
        'if [ "$1" = bundle ]; then\n'
        '  /bin/cat > "$STUB_LOG.stdin"\n'
        # an outdated formula fails `check` unless upgrades are off: the rerun would then upgrade
        '  if [ "$2" = check ]; then [ -n "$HOMEBREW_BUNDLE_NO_UPGRADE" ] && '
        '[ -e "$STUB_LOG.bundled" ]; exit $?; fi\n'
        '  [ -n "$STUB_SAY" ] && echo BREWSTREAM\n'
        '  : > "$STUB_LOG.bundled"\nfi\n')
LABEL = "dev.capsule.capsule-corp-keeper-watch"
BUNDLE_RUN = "brew bundle --file="
ALLOWED = ("capsule-home", "Library/LaunchAgents", ".local/bin")
# one word per step: a run that lists them all is not a silent run
STEP_WORDS = ("Brewfile", "logs folder", "host.json", "bin/capsule", LABEL, "worker image")


class _Install(_World):
    """`_World` with a stateful brew stub, CAPSULE_HOME inside the temp HOME, and `install()`."""

    def setUp(self):
        super().setUp()
        self.home = self.user / "capsule-home"          # inside HOME: the snapshot sees it
        self.docker_app = self.tmp / "Docker.app"       # not there: no real /Applications probe

    def stub(self, tool):
        if tool != "brew":
            return super().stub(tool)
        path = self.bin / "brew"
        path.write_text("#!/bin/sh\n" + BREW)
        path.chmod(0o755)

    def snapshot(self):
        """{relative path: (kind, sha1 or link target, mode, mtime)} for everything under HOME."""
        out = {}
        for p in sorted(self.user.rglob("*")):
            rel = str(p.relative_to(self.user))
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_file():
                out[rel] = ("file", hashlib.sha1(p.read_bytes()).hexdigest(),
                            p.stat().st_mode, p.stat().st_mtime_ns)
            else:
                out[rel] = ("dir",)
        return out

    def install(self, *flags, env=None, system="Darwin", answers=(), free=10**12):
        """(exit code, stdout lines, stderr text) of `capsule install <flags>` in this world."""
        world = {"PATH": str(self.bin), "AGENT_OPS_DOCKER_BIN": str(self.bin),
                 "HOME": str(self.user), "CAPSULE_HOME": str(self.home),
                 "STUB_LOG": str(self.log), **(env or {})}
        args = argparse.Namespace(dry_run="--dry-run" in flags, yes="--yes" in flags,
                                  capsule_home=None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, world))
            for name in ("CAPSULE_LAUNCHD_PREFIX", "CAPSULE_SECRETS_BACKEND",
                         "OBSIDIAN_VAULT_PATH", "OPS_WORKER_IMAGE", "OPS_ENV_FILE"):
                os.environ.pop(name, None)
            stack.enter_context(mock.patch.object(install, "DOCKER_APP", self.docker_app))
            stack.enter_context(mock.patch.object(platform, "system", return_value=system))
            stack.enter_context(mock.patch.object(platform, "mac_ver",
                                                  return_value=("15.1", ("", "", ""), "")))
            stack.enter_context(mock.patch.object(doctor.shutil, "disk_usage",
                                                  return_value=mock.Mock(free=free)))
            stack.enter_context(mock.patch("builtins.input", side_effect=list(answers) or EOFError))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = install.handle(args, None)
        return rc, out.getvalue().splitlines(), err.getvalue()

    def outside_allowed(self, before, after):
        """Paths under HOME that are new or changed and sit outside the three allowed folders."""
        changed = [p for p in after if before.get(p) != after[p]]
        return [p for p in changed
                if not any(p == a or p.startswith(a + "/") or a.startswith(p + "/")
                           for a in ALLOWED)]

    @staticmethod
    def statuses(lines):
        return {ln.split(" ", 1)[0] for ln in lines}


class Steps(_Install):
    def test_dry_run_lists_every_step_and_changes_nothing(self):
        before = self.snapshot()
        rc, lines, _ = self.install("--dry-run")
        self.assertEqual(rc, 0, lines)
        self.assertEqual(self.snapshot(), before)                                  # oracle 2
        text = "\n".join(lines)
        for want in ("WOULD brew bundle (the Brewfile minus the skipped casks)", "WOULD create",
                     "WOULD write", "capsule-home/host.json",
                     "/.local/bin/capsule", f"{LABEL}.plist", "WOULD capsule doctor",
                     "set up per board: run ~/.local/bin/capsule board tick <name>"):
            self.assertIn(want, text)
        # D172: a tester sees only lines they can act on: no maintainer-only job, no dashboards
        for job in sorted(t.name.split(".", 1)[1].removesuffix(".plist")
                          for t in install.TEMPLATES.glob("__PREFIX__.capsule-corp-*.plist")
                          if install.KEEPER not in t.name):
            self.assertNotIn(job, text)
        self.assertNotIn("maintainer", text)
        self.assertNotIn("dashboards", text)
        self.assertNotIn("grafana", text)
        self.assertNotRegex(text, r"\bP\d\d")                                       # no item codes
        self.assertNotIn("capsule-corp-backup", text)
        self.assertFalse([c for c in self.calls() if c.startswith(BUNDLE_RUN)])
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])

    def test_yes_writes_host_json_launcher_and_the_keeper_plist_only(self):
        before = self.snapshot()
        rc, lines, _ = self.install("--yes")
        self.assertEqual(rc, 0, lines)
        after = self.snapshot()
        self.assertEqual(self.outside_allowed(before, after), [])                  # oracle 2
        raw = json.loads((self.home / "host.json").read_text())
        self.assertEqual(raw, {"launchd": {"label_prefix": "dev.capsule"}})
        plist = (self.user / "Library" / "LaunchAgents" / f"{LABEL}.plist").read_text()
        self.assertIn(f"<string>{LABEL}</string>", plist)
        self.assertIn(f"{self.home}/logs/keeper-watch.log", plist)
        self.assertIn(f"<key>CAPSULE_HOME</key><string>{self.home}</string>", plist)
        self.assertIn(f"{paths.REPO}/bin/capsule-keeper-watch.sh", plist)
        self.assertEqual([ln for ln in lines if "dashboards" in ln or "grafana" in ln], [], lines)
        self.assertNotIn("deploy.sh", "\n".join(lines))
        self.assertEqual([c for c in self.calls() if "compose" in c], [])
        self.assertNotRegex(plist, r"__[A-Z_]+__")
        self.assertEqual(sorted(p.name for p in (self.user / "Library/LaunchAgents").iterdir()),
                         [f"{LABEL}.plist"])
        launcher = self.user / ".local" / "bin" / "capsule"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertTrue((self.home / "logs").is_dir())
        self.assertFalse((self.user / ".config").exists())
        self.assertEqual([c for c in self.calls() if c.startswith("launchctl")], [])
        self.assertTrue(any(c.startswith(BUNDLE_RUN) for c in self.calls()))
        self.assertTrue(any("launchctl bootstrap gui/" in ln and "not loaded" in ln
                            for ln in lines))

    def test_the_launcher_runs_the_capsule_cli(self):
        self.install("--yes")
        launcher = self.user / ".local" / "bin" / "capsule"
        done = subprocess.run(["/bin/sh", str(launcher), "host", "get", "launchd.label_prefix",
                               "--capsule-home", str(self.home)], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin"}, cwd=self.tmp, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "dev.capsule"), done.stderr)

    def test_a_second_run_changes_nothing_and_prints_only_ok_or_skip_lines(self):
        self.install("--yes")
        before, calls = self.snapshot(), len(self.calls())
        rc, lines, _ = self.install("--yes")
        self.assertEqual(rc, 0, lines)
        steps = lines[:lines.index(next(ln for ln in lines if ln.startswith("ok macOS")))]
        self.assertTrue(steps)
        self.assertLessEqual(self.statuses(steps), {"ok", "skip", "next"}, steps)
        self.assertEqual(self.snapshot(), before)                                  # oracle 2
        self.assertFalse([c for c in self.calls()[calls:] if c.startswith(BUNDLE_RUN)])

    def test_without_yes_a_no_answer_changes_nothing(self):
        before = self.snapshot()
        _, lines, _ = self.install()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue([ln for ln in lines if ln.endswith("(declined)")])
        self.assertFalse([c for c in self.calls() if c.startswith(BUNDLE_RUN)])

    def test_a_yes_answer_is_honoured_per_step(self):
        rc, lines, _ = self.install(answers=["y", "y", "y", "n", "n"])
        self.assertTrue((self.home / "host.json").is_file())
        self.assertFalse((self.user / ".local" / "bin" / "capsule").exists())
        self.assertTrue(any(c.startswith(BUNDLE_RUN) for c in self.calls()), (rc, lines))

    def test_linux_prints_a_pointer_on_stderr_and_exits_2(self):
        before = self.snapshot()
        rc, lines, err = self.install("--yes", system="Linux")
        self.assertEqual(rc, 2)
        self.assertEqual(lines, [])
        self.assertIn("docs/SETUP.md", err)
        self.assertEqual((self.calls(), self.snapshot()), ([], before))

    def test_an_existing_host_json_is_kept_and_its_prefix_is_used(self):
        self.home.mkdir()
        (self.home / "host.json").write_text('{"launchd": {"label_prefix": "com.mine"}}')
        rc, _, _ = self.install("--yes")
        self.assertEqual(rc, 0)
        self.assertEqual((self.home / "host.json").read_text(),
                         '{"launchd": {"label_prefix": "com.mine"}}')
        agents = self.user / "Library" / "LaunchAgents"
        self.assertTrue((agents / "com.mine.capsule-corp-keeper-watch.plist").is_file())

    def test_a_broken_host_json_is_never_overwritten(self):
        self.home.mkdir()
        (self.home / "host.json").write_text('{"nonsense": 1}')
        rc, lines, _ = self.install("--yes")
        self.assertEqual(rc, 1)
        self.assertTrue([ln for ln in lines if ln.startswith("FAILED host.json")])
        self.assertEqual((self.home / "host.json").read_text(), '{"nonsense": 1}')

    def test_a_foreign_capsule_file_in_local_bin_is_left_alone(self):
        mine = self.user / ".local" / "bin" / "capsule"
        mine.parent.mkdir(parents=True)
        mine.write_text("#!/bin/sh\necho mine\n")
        _, lines, _ = self.install("--yes")
        self.assertEqual(mine.read_text(), "#!/bin/sh\necho mine\n")
        self.assertTrue([ln for ln in lines if ln.startswith("skip") and "is not ours" in ln])

    def test_missing_homebrew_fails_but_the_other_steps_still_run(self):
        (self.bin / "brew").unlink()
        rc, lines, _ = self.install("--yes")
        self.assertEqual(rc, 1)
        self.assertTrue([ln for ln in lines if ln.startswith("FAILED Homebrew not found")])
        self.assertTrue((self.home / "host.json").is_file())

    def test_a_missing_worker_image_prints_the_build_command_and_builds_nothing(self):
        _, lines, _ = self.install("--yes", env={"DOCKER_IMAGE_RC": "1"})
        want = "docker/build-base.sh base && docker/build-base.sh python"
        found = [ln for ln in lines if ln.startswith("next") and want in ln]
        self.assertTrue(found, lines)
        self.assertIn(f"export CAPSULE_HOME={self.home} && {want}", found[0])   # a non-default home
        self.assertIn("40 GB", found[0])
        self.assertIn(f"{self.home}/images.lock", found[0])
        self.assertEqual([c for c in self.calls() if c.startswith("docker")
                          and not c.startswith(("docker version", "docker image inspect"))], [])

    def test_no_record_is_not_built_even_when_docker_has_every_image(self):
        gone = self.user / "no-such-lock"       # no record and no tracked row: a tester's clone
        with mock.patch.object(docker, "LOCK_FILE", gone):
            _, lines, _ = self.install("--yes")  # the stub's `image inspect` says yes to anything
        self.assertTrue([ln for ln in lines if ln.startswith("next worker image not built")], lines)

    def test_the_retired_image_pin_reader_is_gone(self):
        self.assertFalse(hasattr(doctor, "_pin"))

    def test_the_maintainers_tracked_lock_row_does_not_count_as_present(self):
        lock = self.user / "IMAGES.lock"
        row = "capsule-python@sha256:" + "b" * 64
        lock.write_text(row + "\n")
        with mock.patch.object(docker, "LOCK_FILE", lock):
            _, lines, _ = self.install("--yes")
        self.assertFalse([ln for ln in lines if "worker image present" in ln], lines)
        self.assertTrue([ln for ln in lines if ln.startswith("next worker image not built")])

    def test_a_recorded_image_that_is_here_is_present(self):
        self.home.mkdir(parents=True)
        row = "capsule-python@sha256:" + "a" * 64
        (self.home / "images.lock").write_text(row + "\n")
        _, lines, _ = self.install("--yes")
        self.assertIn(f"ok worker image present ({row})", lines)

    def test_the_command_exists_and_names_its_flags(self):
        done = subprocess.run([sys.executable, "-m", "capsule", "install", "--help"],
                              capture_output=True, text=True, cwd=paths.REPO, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue("--dry-run" in done.stdout and "--yes" in done.stdout)


class Brewfile(unittest.TestCase):
    def entries(self):
        text = (paths.REPO / "Brewfile").read_text()
        return re.findall(r'^(brew|cask|tap)\s+"([^"]+)"', text, re.M)

    def test_only_default_taps_and_exactly_what_doctor_names(self):
        entries = self.entries()
        self.assertNotIn("tap", [kind for kind, _ in entries])                      # no third taps
        names = {name for _, name in entries}
        doctor_names = set(re.findall(r"brew install (?:--cask )?([a-z][\w@.-]*)",
                                      Path(doctor.__file__).read_text()))
        # contract "Brewfile and doctor names": doctor literals plus the DEFAULT engine cask
        default_cask = docker_engine.ENGINES[docker_engine.DEFAULT]["cask"]
        self.assertEqual(names, doctor_names | {default_cask})
        # doctor's Docker hint comes from the engine table, never a literal cask name
        self.assertNotIn(default_cask, doctor_names)

    def test_it_is_the_file_the_installer_checks(self):
        self.assertTrue((paths.REPO / "Brewfile").is_file())


class Shim(unittest.TestCase):
    def test_install_sh_is_at_most_50_lines_and_syntactically_valid(self):
        path = paths.REPO / "install.sh"
        self.assertLessEqual(len(path.read_text().splitlines()), 50)
        self.assertEqual(subprocess.run(["sh", "-n", str(path)]).returncode, 0)


CASES = {
    "dry-run-changes-nothing": (
        {"check": "install", "mode": "--dry-run",
         "world": "empty temp HOME and CAPSULE_HOME, stub brew docker launchctl"},
        {"exit": 0, "files_changed": 0, "steps_listed": True, "launchctl_calls": 0}),
    "second-run-only-ok-or-skip": (
        {"check": "install", "mode": "--yes, run twice",
         "world": "empty temp HOME and CAPSULE_HOME, stub brew docker launchctl"},
        {"second_exit": 0, "second_run_changes": 0, "second_run_done_or_would_lines": 0,
         "steps_listed": True}),
    "writes-only-allowed-dirs": (
        {"check": "install", "mode": "--yes",
         "world": "empty temp HOME with CAPSULE_HOME inside it, stub brew docker launchctl",
         "covers": "HOME only; the repo and the temp folder are covered by a unit test"},
        {"files_outside_allowed": 0, "host_json_prefix": "dev.capsule", "plists_written": 1,
         "launcher_executable": True, "launchctl_calls": 0}),
}


class IntentCases(_Install):
    def observe(self, document):
        before = self.snapshot()
        flags = ("--dry-run",) if document["mode"] == "--dry-run" else ("--yes",)
        rc, lines, _ = self.install(*flags)
        after = self.snapshot()
        launchctl = len([c for c in self.calls() if c.startswith("launchctl")])
        if document["mode"] == "--dry-run":
            return {"exit": rc, "launchctl_calls": launchctl,
                    "files_changed": len([p for p in after if before.get(p) != after[p]]),
                    "steps_listed": all(any(w in ln for ln in lines) for w in
                                        ("brew bundle", "host.json", "bin/capsule", LABEL))}
        if document["mode"] == "--yes, run twice":
            mid = self.snapshot()
            rc2, again, _ = self.install("--yes")
            end = self.snapshot()
            return {"second_exit": rc2,
                    "second_run_changes": len([p for p in end if mid.get(p) != end[p]]),
                    "second_run_done_or_would_lines": len(
                        [ln for ln in again if ln.split(" ", 1)[0] in ("done", "WOULD")]),
                    "steps_listed": all(any(w in ln for ln in again) for w in STEP_WORDS)}
        plists = list((self.user / "Library" / "LaunchAgents").glob("*.plist"))
        prefix = json.loads((self.home / "host.json").read_text())["launchd"]["label_prefix"]
        return {"files_outside_allowed": len(self.outside_allowed(before, after)),
                "host_json_prefix": prefix, "plists_written": len(plists),
                "launcher_executable": os.access(self.user / ".local/bin/capsule", os.X_OK),
                "launchctl_calls": launchctl}

    def assert_case(self, name, document, want):
        got = self.observe(document)
        self.assertTrue(intent.matches(want, got), f"{name}: wanted {want}, got {got}")

    def test_the_built_in_expectations(self):
        for name, (document, want) in CASES.items():
            with self.subTest(name):
                self.setUp()
                self.assert_case(name, document, want)

    def test_the_shipped_rows(self):
        if not intent.role_path("install").is_file():
            self.skipTest("contracts/intent/install.jsonl is not shipped yet")
        rows = intent.load("install")
        self.assertEqual({r["id"] for r in rows}, set(CASES))
        for row in rows:
            with self.subTest(row["id"]):
                self.setUp()
                self.assert_case(row["id"], row["given"]["document"], row["expect"])


if __name__ == "__main__":
    unittest.main()
