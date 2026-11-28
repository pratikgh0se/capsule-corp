"""P08 `capsule install`: the Opus review's fixes (same fake world as test_install.py).

Covers: a `capsule/` folder in the current directory never shadows the clone (shim and launcher),
a different or symlinked keeper plist is never replaced, a non-default CAPSULE_HOME reaches the
launcher, casks installed outside brew are left out of the bundle, brew output is streamed and
auto-update is off, and a run writes nothing in the repo or the temp folder."""
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from capsule import paths
from capsule.tests.test_install import BUNDLE_RUN, LABEL, _Install


class Hardening(_Install):
    def plant(self):
        """A folder holding a `capsule/` package that writes a marker file when it is imported."""
        evil, marker = self.tmp / "evil", self.tmp / "pwned"
        (evil / "capsule").mkdir(parents=True)
        (evil / "capsule" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
        (evil / "capsule" / "__main__.py").write_text("")
        return evil, marker

    def launcher_text(self):
        return (self.user / ".local" / "bin" / "capsule").read_text()

    def launch(self, *args, cwd=None, env=None):
        """The installed launcher, run with a bare PATH and a HOME that holds nothing."""
        world = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp / "nohome"), **(env or {})}
        return subprocess.run(["/bin/sh", str(self.user / ".local" / "bin" / "capsule"), *args],
                              capture_output=True, text=True, env=world, cwd=cwd or self.tmp,
                              timeout=60)

    def agents_plist(self):
        return self.user / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def test_the_shim_never_runs_a_capsule_folder_from_the_current_directory(self):
        evil, marker = self.plant()
        shim_bin = self.tmp / "shimbin"
        shim_bin.mkdir()
        (shim_bin / "python3").symlink_to(sys.executable)
        (shim_bin / "uname").write_text("#!/bin/sh\necho Darwin\n")
        (shim_bin / "uname").chmod(0o755)
        world = {"PATH": f"{shim_bin}:{self.bin}:/usr/bin:/bin", "HOME": str(self.user),
                 "CAPSULE_HOME": str(self.home), "STUB_LOG": str(self.log),
                 "AGENT_OPS_DOCKER_BIN": str(self.bin)}
        done = subprocess.run(["/bin/sh", str(paths.REPO / "install.sh"), "--dry-run"],
                              capture_output=True, text=True, env=world, cwd=evil, timeout=60)
        said = done.stdout + done.stderr
        self.assertFalse(marker.exists(), said)                                    # oracle 2
        self.assertIn("WOULD capsule doctor" if platform.system() == "Darwin"
                      else "macOS-only", said)                                     # oracle 1

    def test_the_launcher_never_runs_a_capsule_folder_from_the_current_directory(self):
        self.install("--yes")
        evil, marker = self.plant()
        done = self.launch("host", "get", "launchd.label_prefix", cwd=evil)
        self.assertFalse(marker.exists(), done.stderr)                             # oracle 2
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "dev.capsule"), done.stderr)

    def test_a_different_keeper_plist_is_left_alone_even_with_yes(self):
        self.agents_plist().parent.mkdir(parents=True)
        self.agents_plist().write_text("<plist>mine</plist>")
        _, lines, _ = self.install("--yes")
        self.assertEqual(self.agents_plist().read_text(), "<plist>mine</plist>")
        self.assertTrue([ln for ln in lines if ln.startswith("skip") and "differs: move it, "
                         "then rerun" in ln], lines)
        self.assertFalse([ln for ln in lines if "launchctl bootstrap" in ln])

    def test_a_symlinked_keeper_plist_is_left_alone_even_with_yes(self):
        target = self.tmp / "elsewhere.plist"
        target.write_text("<plist>theirs</plist>")
        self.agents_plist().parent.mkdir(parents=True)
        self.agents_plist().symlink_to(target)
        _, lines, _ = self.install("--yes")
        self.assertTrue(self.agents_plist().is_symlink())
        self.assertEqual(target.read_text(), "<plist>theirs</plist>")
        self.assertTrue([ln for ln in lines if "differs: move it, then rerun" in ln], lines)

    def test_a_non_default_home_reaches_the_launcher_unless_the_caller_sets_one(self):
        other = self.tmp / "other"
        other.mkdir()
        (other / "host.json").write_text('{"launchd": {"label_prefix": "com.theirs"}}')
        for home in (self.user / "capsule-home", self.user / "it's a home & more"):
            with self.subTest(home=str(home)):
                self.home = home
                home.mkdir()
                (home / "host.json").write_text('{"launchd": {"label_prefix": "com.mine"}}')
                self.install("--yes")
                done = self.launch("host", "get", "launchd.label_prefix")
                self.assertEqual((done.returncode, done.stdout.strip()), (0, "com.mine"),
                                 done.stderr)
                done = self.launch("host", "get", "launchd.label_prefix",
                                   env={"CAPSULE_HOME": str(other)})
                self.assertEqual(done.stdout.strip(), "com.theirs")                # caller wins

    def test_the_default_home_is_not_baked_into_the_launcher(self):
        default = self.user / ".local" / "share" / "capsule"
        self.install("--yes", env={"CAPSULE_HOME": str(default)})
        self.assertNotIn("CAPSULE_HOME", self.launcher_text())
        self.assertIn("exec ", self.launcher_text())

    def brewfile_sent(self):
        return Path(f"{self.log}.stdin").read_text()

    def casks_sent(self):
        return re.findall(r'^cask "([^"]+)"', self.brewfile_sent(), re.M)

    def test_a_cask_whose_tool_is_already_installed_is_left_out_of_the_bundle(self):
        _, lines, _ = self.install("--yes")
        self.assertEqual(self.casks_sent(), [])
        self.assertIn('brew "uv"', self.brewfile_sent())
        self.assertEqual(len([ln for ln in lines if ln.startswith("skip cask ")]), 2, lines)
        self.assertIn('cask "orbstack"', (paths.REPO / "Brewfile").read_text())  # untouched

    def test_a_cask_whose_tool_is_missing_stays_in_the_bundle(self):
        for tool, cask in (("claude", "claude-code@latest"), ("docker", "orbstack")):
            with self.subTest(tool):
                self.setUp()
                (self.bin / tool).unlink()
                # docker's own door still needs a fake docker dir, which is not on the PATH
                fake = self.tmp / "fake-docker"
                fake.mkdir()
                (fake / "docker").write_text("#!/bin/sh\nexit 1\n")
                (fake / "docker").chmod(0o755)
                self.install("--yes", env={"AGENT_OPS_DOCKER_BIN": str(fake)})
                self.assertEqual(self.casks_sent(), [cask])      # the other tool is on the PATH

    def test_docker_counts_as_installed_when_its_app_is_in_applications(self):
        for present in (True, False):
            with self.subTest(app_present=present):
                self.setUp()
                (self.bin / "docker").unlink()
                fake = self.tmp / "fake-docker"
                fake.mkdir()
                (fake / "docker").write_text("#!/bin/sh\nexit 1\n")
                (fake / "docker").chmod(0o755)
                if present:
                    self.docker_app.mkdir()
                _, lines, _ = self.install("--yes", env={"AGENT_OPS_DOCKER_BIN": str(fake)})
                self.assertEqual(self.casks_sent(), [] if present else ["orbstack"])
                self.assertEqual(bool([ln for ln in lines if ln.startswith("skip cask orbstack")]),
                                 present)

    def test_the_skip_line_says_already_installed_without_naming_brew(self):
        _, lines, _ = self.install("--yes")
        skips = [ln for ln in lines if ln.startswith("skip cask ")]
        self.assertEqual(len(skips), 2, lines)
        self.assertTrue(all(ln.endswith("is already installed") for ln in skips), skips)

    def test_the_prompt_does_not_claim_a_file_it_does_not_pass(self):
        _, lines, _ = self.install()
        want = "skip brew bundle (the Brewfile minus the skipped casks) (declined)"
        self.assertTrue([ln for ln in lines if ln.startswith(want)], lines)
        self.assertFalse([ln for ln in lines if "--file=" in ln], lines)

    def test_every_brew_call_turns_upgrades_off_so_a_rerun_upgrades_nothing(self):
        self.install("--yes")
        before = len([c for c in self.calls() if c.startswith("brew ")])
        marks = Path(f"{self.log}.noupgrade").read_text().split()
        self.assertEqual((len(marks), set(marks)), (before, {"1"}))
        rc, lines, _ = self.install("--yes")                  # an outdated formula fails `check`
        self.assertEqual(rc, 0, lines)                        # unless upgrades are off: no bundle
        self.assertEqual(len([c for c in self.calls() if c.startswith(BUNDLE_RUN)]), 1)

    def test_every_brew_call_turns_auto_update_off(self):
        self.install("--yes")
        marks = Path(f"{self.log}.noauto").read_text().split()
        self.assertEqual(len(marks), len([c for c in self.calls() if c.startswith("brew ")]))
        self.assertEqual(set(marks), {"1"})

    def test_the_brew_bundle_output_is_streamed_not_captured(self):
        if platform.system() != "Darwin":
            self.skipTest("runs the real entry point, which is macOS-only")
        world = {"PATH": f"{self.bin}:/usr/bin:/bin", "AGENT_OPS_DOCKER_BIN": str(self.bin),
                 "HOME": str(self.user), "CAPSULE_HOME": str(self.home),
                 "STUB_LOG": str(self.log), "STUB_SAY": "1"}
        done = subprocess.run([sys.executable, "-m", "capsule", "install", "--yes"],
                              capture_output=True, text=True, env=world, cwd=paths.REPO,
                              timeout=120)
        self.assertIn("BREWSTREAM", done.stdout, done.stderr)

    def tree(self, root):
        out = {}
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for name in files:
                stat = (Path(dirpath) / name).lstat()
                out[os.path.join(dirpath, name)] = (stat.st_size, stat.st_mtime_ns)
        return out

    def test_a_run_writes_nothing_in_the_repo_or_the_temp_folder(self):
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        before = self.tree(paths.REPO)
        with mock.patch.object(tempfile, "tempdir", str(scratch)):
            self.install("--yes", env={"TMPDIR": str(scratch)})
        self.assertEqual(self.tree(paths.REPO), before)
        self.assertEqual(list(scratch.iterdir()), [])
