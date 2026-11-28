"""landing_prep.py fail-closed paths (loophole hunt rounds): a failing grep or merge-tree, a branch
that moves mid-run, the marker written last, an unpinned intent file. Same temp-repo fixture as
test_landing_prep.py; oracle is the git state and files on disk after each run, never the
script's own verdicts."""
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import landing_prep as lp  # noqa: E402
import test_landing_prep as base  # noqa: E402


class Safety(base.Prep):
    def faulty(self, sub):
        """git as the script sees it, except a call with `sub` among its args fails (rc 128)."""
        real = lp.git

        def fake(cwd, *args, check=True):
            if sub in args:
                return subprocess.CompletedProcess(args, 128, "", "fatal: boom\nmore")
            return real(cwd, *args, check=check)
        return mock.patch.object(lp, "git", fake)

    def test_marker_scan_error_fails_closed(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        with self.faulty("grep"):
            rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("marker scan failed", err)
        self.assertIn("fatal: boom", err)
        self.assertFalse(self.out.exists())

    def test_preview_merge_tree_error_is_not_none(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        with self.faulty("merge-tree"):
            rc, out, _ = self.run_prep([("build/a", m)], apply=False)
        self.assertEqual(rc, 0)
        self.assertIn("conflicts: conflict check failed: fatal: boom;", out)
        self.assertNotIn("conflicts: none", out)

    def test_branch_moved_during_the_run_is_refused(self):
        m = self.branch("build/a", {"a.py": "a\n"})

        def move(*args, **kw):
            self.g(self.repo, "switch", "-q", "build/a")
            self.write(self.repo, "late.py", "late\n")
            self.g(self.repo, "add", "-A")
            self.g(self.repo, "commit", "-q", "-m", "late push")
            self.g(self.repo, "switch", "-q", "main")
            return set(), []
        with mock.patch.object(lp, "_squash", side_effect=move):
            rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("branch build/a moved during the run", err)
        self.assertFalse(self.out.exists())

    def test_squash_merges_the_reviewed_commit_not_the_name(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        real = lp.git

        def late_push(cwd, *args, check=True):
            if "--squash" in args:      # the branch name now points somewhere else
                self.g(self.repo, "branch", "-q", "-f", "build/a", "main")
            return real(cwd, *args, check=check)
        with mock.patch.object(lp, "git", late_push):
            rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("moved during the run", err)
        self.assertEqual((self.wt / "a.py").read_text(), "a\n")
        self.assertEqual(self.g(self.wt, "log", "-1", "--format=%s"), "msg for build/a")

    def test_log_failure_leaves_no_marker(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        real = lp._log

        def flaky(**row):
            if row["what"] == "write":
                raise OSError("disk full")
            return real(**row)
        with mock.patch.object(lp, "_log", flaky):
            rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("disk full", err)
        self.assertTrue((self.out / "keeper-brief-b5.md").exists())
        self.assertFalse((self.out / "b5-review-ok").exists())

    def test_unpinned_tracked_intent_file_refused(self):
        m = self.branch("build/a", {"contracts/intent/loose.jsonl": '{"n": 3}\n'})
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("not pinned in SHA256SUMS: loose.jsonl", err)
        self.assertFalse(self.out.exists())

    def test_nested_intent_sums_are_checked_too(self):
        """contracts/intent/lookout/SHA256SUMS: a pin that no longer matches, or a loose file."""
        sub = "contracts/intent/lookout"
        good = '{"n": 9}\n'
        pin = f"{hashlib.sha256(good.encode()).hexdigest()}  one.jsonl\n"
        self.branch("build/seed", {f"{sub}/one.jsonl": good, f"{sub}/SHA256SUMS": pin})
        self.g(self.repo, "merge", "-q", "--no-edit", "build/seed")
        self.g(self.repo, "push", "-q", "origin", "main")
        stale = self.branch("build/stale", {f"{sub}/one.jsonl": '{"n": 10}\n'})
        rc, _, err = self.run_prep([("build/stale", stale)])
        self.assertEqual(rc, 1)
        self.assertIn("SHA256SUMS check failed", err)
        self.assertIn("one.jsonl", err)
        self.assertFalse(self.out.exists())
        self.g(self.repo, "worktree", "remove", "--force", self.wt)
        self.g(self.repo, "update-ref", "-d", "refs/heads/keeper/batch5")
        loose = self.branch("build/loose", {f"{sub}/two.jsonl": '{"n": 11}\n'})
        rc, _, err = self.run_prep([("build/loose", loose)])
        self.assertEqual(rc, 1)
        self.assertIn("not pinned in lookout/SHA256SUMS: two.jsonl", err)


class IntentRows(base.Prep):
    """Z1 (row 643): contracts/intent/landing_prep.jsonl, replayed once the CoS has stamped it,
    the same pattern as test_storage_gc_tmp.IntentRows. The built-in cases (real temp git repos)
    run with or without the file."""
    CASES = {"sums-conflict-unioned-and-flagged": "sums", "unapproved-batch-refused-untouched":
             "unapproved", "unexpected-conflict-stops-without-marker": "conflict"}

    def sums_branch(self, name, new):
        rows = f'{{"n": "{new}"}}\n'
        pins = f"{base.sha(self.repo / 'contracts/intent/alpha.jsonl')}  alpha.jsonl\n" \
               f"{hashlib.sha256(rows.encode()).hexdigest()}  {new}.jsonl\n"
        return (name, self.branch(name, {"contracts/intent/SHA256SUMS": pins,
                                         f"contracts/intent/{new}.jsonl": rows}))

    def leftovers(self):
        gone = (not self.wt.exists(), not (self.out / "b5-review-ok").exists(),
                not (self.out / "keeper-brief-b5.md").exists())
        return {"worktree_exists": int(not gone[0]), "marker_exists": int(not gone[1]),
                "brief_exists": int(not gone[2])}

    def replay(self, case):
        if case == "sums":
            rc, out, _ = self.run_prep([self.sums_branch("build/a", "bravo"),
                                        self.sums_branch("build/b", "charlie")])
            note = "auto-unioned, unreviewed: contracts/intent/SHA256SUMS"
            brief = self.out / "keeper-brief-b5.md"
            log = (self.tmp / "state" / "landing_prep.jsonl").read_text().splitlines()
            order = [Path(json.loads(ln)["id"]).name for ln in log if '"write"' in ln]
            return {"exit": rc, "branches_squashed": int(self.g(
                        self.wt, "rev-list", "--count", "origin/main..HEAD")),
                    "unioned_flagged_unreviewed": int(note in out and note in brief.read_text()),
                    "brief_written": int(brief.exists()),
                    "marker_written": int((self.out / "b5-review-ok").exists()),
                    "marker_written_after_brief": int(order == [brief.name, "b5-review-ok"])}
        if case == "unapproved":
            m = self.branch("build/a", {"a.py": "a\n"})
            rc, _, _ = self.run_prep([("build/a", m)], approved="looks fine")
            return {"exit": rc, **self.leftovers(), "branch_exists": int(bool(
                self.g(self.repo, "branch", "--list", "keeper/*")))}
        a = self.branch("build/a", {"x.py": "one\nAAA\nthree\n"})
        b = self.branch("build/b", {"x.py": "one\nBBB\nthree\n"})
        rc, _, err = self.run_prep([("build/a", a), ("build/b", b)])
        left = self.leftovers()
        return {"exit": rc, "stopped_on_unexpected_conflict": int("unexpected conflict" in err
                                                                  and "x.py" in err),
                "marker_exists": left["marker_exists"], "brief_exists": left["brief_exists"]}

    def test_the_built_in_cases_hold_without_the_file(self):
        got = self.replay("sums")
        self.assertEqual(got, {"exit": 0, "branches_squashed": 2, "unioned_flagged_unreviewed": 1,
                               "brief_written": 1, "marker_written": 1,
                               "marker_written_after_brief": 1})

    def test_unapproved_case_holds(self):
        self.assertEqual(self.replay("unapproved"),
                         {"exit": 1, "worktree_exists": 0, "marker_exists": 0, "brief_exists": 0,
                          "branch_exists": 0})

    def test_conflict_case_holds(self):
        self.assertEqual(self.replay("conflict"),
                         {"exit": 1, "stopped_on_unexpected_conflict": 1, "marker_exists": 0,
                          "brief_exists": 0})

    def test_the_intent_rows_hold_when_the_file_exists(self):
        from capsule import intent
        rows = intent.load("landing_prep")
        self.assertEqual({r["id"] for r in rows}, set(self.CASES))
        for row in rows:
            self.setUp()        # a fresh temp repo per case: no branch or worktree leaks over
            got = self.replay(self.CASES[row["id"]])
            for key, want in row["expect"].items():
                if isinstance(want, int):
                    self.assertEqual(got[key], want, row["id"])


if __name__ == "__main__":
    unittest.main()
