"""t_0f123965: the intent lint's diff base prefers `origin/main` over a clone's local `main`.

Factory clones never advance their local `main`, so it goes stale while `origin/main` moves; the
"at most three new examples per round" lint then counted rows that had already landed. These tests
build a throwaway repo with a stale local `main` and a newer `origin/main` carrying intent rows.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import intent, paths
from capsule.tests import test_intent as ti

NAMES = ti.ROLES + intent.STANDALONE
CAP_TEST = "test_at_most_three_new_examples_per_round_since_the_merge_base"


def _git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _rows(role, n):
    return "".join(json.dumps({"id": f"{role}-{i}"}) + "\n" for i in range(n))


class DiffBase(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.origin, self.clone = tmp / "origin.git", tmp / "clone"
        _git(tmp, "init", "--bare", "-b", "main", str(self.origin))
        _git(tmp, "clone", str(self.origin), str(self.clone))
        self.intent = self.clone / "contracts" / "intent"
        self.intent.mkdir(parents=True)
        for name in NAMES:
            (self.intent / f"{name}.jsonl").write_text(_rows(name, 1))
        self.commit("seed")
        _git(self.clone, "push", "origin", "HEAD:main")
        self.stale = _git(self.clone, "rev-parse", "HEAD")
        # origin/main moves on (4 rows land for dark_test); the local `main` ref stays behind.
        _git(self.clone, "checkout", "-b", "work")
        (self.intent / "dark_test.jsonl").write_text(_rows("dark_test", 5))
        self.commit("land rows")
        _git(self.clone, "push", "origin", "HEAD:main")
        _git(self.clone, "fetch", "origin")
        self.fresh = _git(self.clone, "rev-parse", "origin/main")
        _git(self.clone, "checkout", "-q", "--detach", "origin/main")
        _git(self.clone, "branch", "-f", "main", self.stale)
        self.patches = [mock.patch.object(paths, "REPO", self.clone),
                        mock.patch.object(paths, "INTENT", self.intent)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def commit(self, msg):
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", msg)

    def run_cap_lint(self):
        result = unittest.TestResult()
        ti.Lint(CAP_TEST).run(result)
        return result

    def test_prefers_origin_main_over_a_stale_local_main(self):
        self.assertNotEqual(self.stale, self.fresh)
        self.assertEqual(ti._role_diff_base(), (self.fresh, True))

    def test_already_landed_rows_count_zero_new(self):
        result = self.run_cap_lint()
        self.assertEqual((result.failures, result.errors, result.skipped), ([], [], []))

    def test_a_genuine_fourth_new_example_still_fails(self):
        (self.intent / "deploy.jsonl").write_text(_rows("deploy", 5))   # 1 on base + 4 new
        result = self.run_cap_lint()
        self.assertEqual(len(result.failures), 1)
        self.assertIn("deploy", result.failures[0][1])

    def test_falls_back_to_local_main_when_there_is_no_origin(self):
        _git(self.clone, "remote", "remove", "origin")
        self.assertEqual(ti._role_diff_base(),
                         (_git(self.clone, "merge-base", "HEAD", "main"), False))

    def test_origin_without_origin_main_fails_the_cap_test_instead_of_using_local_main(self):
        """t_30645655: a clone whose `origin/main` ref is missing (or a stale ref view) but which
        has a local `main`; the old fallback silently took the local `main`."""
        _git(self.clone, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(ti._role_diff_base(), (None, True))
        result = self.run_cap_lint()
        self.assertEqual((result.errors, result.skipped), ([], []))
        self.assertEqual(len(result.failures), 1)
        self.assertIn("origin/main did not resolve", result.failures[0][1])


if __name__ == "__main__":
    unittest.main()
