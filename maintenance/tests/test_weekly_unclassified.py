# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   WeeklyMdPastesTheCapture.test_pastes_the_placeholder_and_asks_for_a_decision_line
"""Row 470/473 (t_59a6f5af), card done-when: the weekly introspection sees the unclassified
branch list. `run.sh`'s `weekly)` arm - the runner, never the model - captures
`python3 -m capsule unblocked`'s own "-- unclassified" section before the session starts,
capped by the same perl-alarm idiom `with_cap` already uses, and substitutes it into
`PROMPT_TEXT` as `__UNCLASSIFIED__`, the same runner-side placeholder pattern
`__OPS_PY__`/`__MAINT_DIR__`/`__BOARDS__` already use. Round 2, R2-I1: a capture that
failed is its own distinct value (`capture failed (capsule unblocked exit N) - ...`), never
`"none"` - "none" means "nothing to decide", and a broken capture must never read that way.
`weekly.md` pastes whichever value came back verbatim under `## Progress by project` and adds
either `Decide park-or-delete: <branch>` lines or `Check unclassified branches by hand` under
`## Focus for next week` - deciding nothing itself, same as the betting table above it.

`RunnerSubstitution` extracts the exact capture/substitute lines out of `run.sh` (never a
re-typed copy) and runs them for real, so this is proof the mechanism works, not a paraphrase
of it - the one thing a plain string-pin test (dropped here, Z8 round 2) cannot offer.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from capsule.tests import tmpclean  # noqa: E402

tmpclean.install()
RUN_SH = ROOT / "maintenance" / "run.sh"
WEEKLY = ROOT / "maintenance" / "weekly.md"
from capsule.tests.public_tree import private_only  # noqa: E402


class WeeklyMdPastesTheCapture(unittest.TestCase):
    @private_only
    def test_pastes_the_placeholder_and_asks_for_a_decision_line(self):
        text = WEEKLY.read_text(encoding="utf-8")
        self.assertIn("__UNCLASSIFIED__", text)
        self.assertIn("## Progress by project", text)
        self.assertIn("Decide park-or-delete: <branch>", text)
        self.assertRegex(text, r"decision itself is \S+'s")   # decides nothing, same as I1 says

    @private_only
    def test_names_the_capture_failed_fallback(self):
        text = WEEKLY.read_text(encoding="utf-8")
        self.assertIn("capture failed", text)
        self.assertIn("Check unclassified branches by hand", text)


def _git_repo(commits=1, old_commit_days=None):
    """A throwaway `main`-only repo; with `old_commit_days`, one more commit on a checked-out
    branch, back-dated that many days - the fixture both `RunnerSubstitution` tests share."""
    repo = Path(tempfile.mkdtemp())
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    run = lambda *a, e=env: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                            capture_output=True, env=e)
    run("init", "-q", "-b", "main")
    (repo / "f.txt").write_text("1")
    run("add", "f.txt")
    run("commit", "-q", "-m", "init")
    if old_commit_days is not None:
        run("checkout", "-q", "-b", "owners-scratch-idea")
        (repo / "f.txt").write_text("2")
        now = int(subprocess.run(["date", "+%s"], capture_output=True, text=True).stdout)
        old = f"{now - old_commit_days * 86400} +0000"
        run("commit", "-q", "-am", "change", e=dict(env, GIT_AUTHOR_DATE=old, GIT_COMMITTER_DATE=old))
    return repo


def _capsule_home(repo):
    home = Path(tempfile.mkdtemp())
    (home / "config.json").write_text(f'{{"unblocked": {{"boards": [], "repos": ["{repo}"]}}}}')
    return home


class RunnerSubstitution(unittest.TestCase):
    """The mechanism itself: extract the exact `weekly)` arm's capture/substitute lines from
    `run.sh` (never a re-typed copy) and run them for real."""

    def _snippet(self):
        arm = RUN_SH.read_text(encoding="utf-8")
        m = re.search(r'UNCL_RAW=.*?PROMPT_TEXT="\$\{PROMPT_TEXT//__UNCLASSIFIED__/\$UNCLASSIFIED\}"',
                       arm, re.S)
        self.assertIsNotNone(m, "weekly)'s UNCL_RAW capture/substitute lines moved or changed shape")
        return m.group(0)

    def _run(self, capsule_corp_dir, capsule_home, env=None, shell="bash"):
        script = (f'set -u\nCAPSULE_CORP_DIR={capsule_corp_dir}\nCAPSULE_HOME={capsule_home}\n'
                  f'export CAPSULE_HOME\nPROMPT_TEXT="before __UNCLASSIFIED__ after"\n'
                  f'{self._snippet()}\nprintf %s "$PROMPT_TEXT"')
        return subprocess.run([shell, "-c", script], capture_output=True, text=True, timeout=30,
                              env=env)

    def test_placeholder_is_replaced_with_a_flagged_old_branch(self):
        repo = _git_repo(old_commit_days=40)
        out = self._run(ROOT, _capsule_home(repo))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("__UNCLASSIFIED__", out.stdout)          # placeholder is gone
        self.assertIn("-- unclassified", out.stdout)              # the real detector's own heading
        self.assertIn("park-or-delete decision due", out.stdout)  # 40d > 30d threshold
        self.assertIn("before", out.stdout)
        self.assertIn("after", out.stdout)

    def test_placeholder_falls_back_to_none_when_nothing_is_unclassified(self):
        repo = _git_repo()
        out = self._run(ROOT, _capsule_home(repo))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "before none after")

    def test_a_broken_capture_never_reads_as_none(self):
        """R2-I1: `CAPSULE_CORP_DIR` pointing nowhere makes the `cd` fail, so the `&&` never
        runs the detector at all (exit 1, empty output) - that must read as a named failure,
        not "nothing to decide"."""
        out = self._run("/nonexistent-capsule-corp-dir", _capsule_home(_git_repo()))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("capture failed", out.stdout)
        self.assertNotIn("before none after", out.stdout)
        self.assertIn("run `python3 -m capsule unblocked` by hand", out.stdout)

    def _assert_failed(self, out, rc=None):
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("capture failed", out.stdout)
        self.assertNotIn("before none after", out.stdout)
        if rc is not None:
            self.assertIn(f"capsule unblocked exit {rc}", out.stdout)

    def test_a_non_capsule_dir_never_reads_as_none(self):
        """R3-C1 case 1: an existing dir without `capsule/` - `No module named capsule`
        exits 1 with non-empty output, which is not a detector report."""
        self._assert_failed(self._run(tempfile.mkdtemp(), _capsule_home(_git_repo())))

    def test_missing_sed_never_reads_as_none(self):
        """R3-C1 case 2: no `sed` on PATH makes the filtered section empty - not "none"."""
        bin_dir = Path(tempfile.mkdtemp())
        for tool in ("perl", "python3", "git", "grep"):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        out = self._run(ROOT, _capsule_home(_git_repo()), env=dict(os.environ, PATH=str(bin_dir)),
                        shell="/bin/bash")
        self._assert_failed(out)

    def test_missing_grep_names_the_tool_not_a_misleading_exit_0(self):
        """W96 follow-up (t_ae574709): with no `grep` on PATH the detector itself exits 0, so
        "exit 0" alone hides the cause. The message must name the missing tool."""
        bin_dir = Path(tempfile.mkdtemp())
        for tool in ("perl", "python3", "git", "sed"):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        out = self._run(ROOT, _capsule_home(_git_repo()), env=dict(os.environ, PATH=str(bin_dir)),
                        shell="/bin/bash")
        self._assert_failed(out)
        self.assertIn("grep missing", out.stdout)

    def test_a_present_grep_never_reports_grep_missing(self):
        """The other side: a failure with grep installed (a non-capsule dir) must not blame it."""
        out = self._run(tempfile.mkdtemp(), _capsule_home(_git_repo()))
        self._assert_failed(out)
        self.assertNotIn("missing", out.stdout)

    def test_a_detector_exit_2_never_reads_as_none(self):
        """R3-C1 (kills `-le 1` -> `-le 2`): a malformed config.json makes the detector exit 2."""
        home = Path(tempfile.mkdtemp())
        (home / "config.json").write_text("{not json")
        self._assert_failed(self._run(ROOT, home), rc=2)

    def test_a_valid_header_with_exit_2_is_still_a_failure(self):
        """Kills `-le 1` -> `-le 2`: the header gate alone would pass this report-shaped
        output, so the exit status must be checked too (exit 2 = the detector's own error)."""
        bin_dir = Path(tempfile.mkdtemp())
        shim = bin_dir / "python3"
        shim.write_text("#!/bin/sh\necho 'UNBLOCKED 0 UNOWNED 0'\nexit 2\n")
        shim.chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        self._assert_failed(self._run(ROOT, _capsule_home(_git_repo()), env=env), rc=2)

    def test_a_hung_detector_is_capped_not_unbounded(self):
        """R3-I2: a `python3` shim that sleeps past `RUN_SH_UNCL_CAP_S` (1 s here) is killed by
        the perl alarm (SIGALRM, shell exit 142) - never an unbounded wait."""
        bin_dir = Path(tempfile.mkdtemp())
        shim = bin_dir / "python3"
        shim.write_text("#!/bin/sh\nexec sleep 5\n")
        shim.chmod(0o755)
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", RUN_SH_UNCL_CAP_S="1")
        self._assert_failed(self._run(ROOT, _capsule_home(_git_repo()), env=env), rc=142)


class IntentExamples(unittest.TestCase):
    """Z1: every `contracts/intent/weekly_capture.jsonl` example through the real extracted
    snippet, with PATH built to lack exactly the tool the example names."""

    def test_every_example_matches_its_oracle(self):
        import json
        lines = (ROOT / "contracts" / "intent" / "weekly_capture.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            row = json.loads(line)
            without = row["given"]["document"]["path_without"]
            bin_dir = Path(tempfile.mkdtemp())
            for tool in ("env", "perl", "python3", "git", "sed", "grep"):  # env: perl execs it
                if tool != without:
                    (bin_dir / tool).symlink_to(shutil.which(tool))
            out = RunnerSubstitution("_snippet")._run(
                ROOT, _capsule_home(_git_repo()), env=dict(os.environ, PATH=str(bin_dir)), shell="/bin/bash")
            want = row["expect"]
            if want["capture"] == "none":
                self.assertEqual(out.stdout, "before none after", row["id"])
            else:
                self.assertIn("capture failed", out.stdout, row["id"])
                self.assertIn(want["names"], out.stdout, row["id"])


if __name__ == "__main__":
    unittest.main()
