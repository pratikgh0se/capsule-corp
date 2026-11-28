"""landing_prep.py (row 643) on throwaway git repos in a temp dir: a bare origin, a clone named
capsule-corp, branches made by hand. Oracle 1: the git state read back after each run (branches,
worktrees, file contents, shasum). Oracle 2: the literal expectations written here (file lists,
sorted tuple, marker text), never the script's own printed verdicts. No live repo, no docker."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import landing_prep as lp

INTENT_PY = 'STANDALONE = (\n    "alpha",\n    "zulu",\n)\n\n\ndef lint_all():\n' \
            '    bad = [p for p in __import__("pathlib").Path("contracts/intent").glob("*.jsonl")' \
            ' if "BAD" in p.read_text()]\n    return [str(p) for p in bad]\n'
APPROVED = "APPROVE test batch"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Prep(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "false",
               "AGENT_OPS_STATE_DIR": str(self.tmp / "state")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.tmp / "o.git")])
        self.repo = self.tmp / "work" / "capsule-corp"
        self.repo.parent.mkdir()
        subprocess.run(["git", "clone", "-q", str(self.tmp / "o.git"), str(self.repo)],
                       capture_output=True)
        self.write(self.repo, "capsule/__init__.py", "")
        self.write(self.repo, "capsule/intent.py", INTENT_PY)
        self.write(self.repo, "capsule/__main__.py", "a = 1\n")
        self.write(self.repo, "x.py", "one\ntwo\nthree\n")
        self.write(self.repo, "contracts/intent/alpha.jsonl", '{"n": 1}\n')
        self.write(self.repo, "contracts/intent/SHA256SUMS",
                   f"{sha(self.repo / 'contracts/intent/alpha.jsonl')}  alpha.jsonl\n")
        self.g(self.repo, "add", "-A")
        self.g(self.repo, "commit", "-q", "-m", "base")
        self.g(self.repo, "push", "-q", "-u", "origin", "main")
        self.out = self.tmp / "out"
        self.wt = self.repo.parent / "capsule-corp-keeper-batch5"

    def g(self, cwd, *args):
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def write(self, root, rel, text):
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def branch(self, name, files, msg=None):
        """A branch off main with the given {path: text} files and a message file."""
        self.g(self.repo, "switch", "-q", "-c", name, "main")
        for rel, text in files.items():
            self.write(self.repo, rel, text)
        self.g(self.repo, "add", "-A")
        self.g(self.repo, "commit", "-q", "-m", name)
        self.g(self.repo, "switch", "-q", "main")
        path = self.tmp / (name.replace("/", "_") + ".txt")
        path.write_text(msg or f"msg for {name}\n\nCo-Authored-By: T <t@t>\n")
        return str(path)

    def run_prep(self, branches, extra=(), approved=APPROVED, apply=True):
        argv = ["--batch", "5", "--prefix", "b5", "--approved", approved, "--repo", str(self.repo),
                "--out-dir", str(self.out), *extra]
        for br, msg in branches:
            argv += ["--branch", br, "--msg", msg]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = lp.main(argv + (["--apply"] if apply else []))
        return rc, out.getvalue(), err.getvalue()

    def assert_untouched(self):
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.g(self.repo, "branch", "--list", "keeper/*"), "")
        self.assertFalse(self.out.exists())


class Rules(Prep):
    def test_preview_changes_nothing_and_lists_files(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        rc, out, _ = self.run_prep([("build/a", m)], apply=False)
        self.assertEqual(rc, 0)
        self.assertIn("a.py", out)
        self.assertIn("conflicts: none", out)
        self.assertNotIn("{{", out)
        self.assert_untouched()

    def test_preview_predicts_conflict(self):
        a = self.branch("build/a", {"x.py": "one\nAAA\nthree\n"})
        b = self.branch("build/b", {"x.py": "one\nBBB\nthree\n"})
        self.g(self.repo, "merge", "-q", "--no-edit", "build/a")
        self.g(self.repo, "push", "-q", "origin", "main")
        _, out, _ = self.run_prep([("build/b", b)], apply=False)
        self.assertIn("x.py (WOULD STOP)", out)
        self.assertTrue(a)

    def test_refuses_unapproved(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        for bad in ("approve me", "OK", ""):
            rc, _, err = self.run_prep([("build/a", m)], approved=bad)
            self.assertEqual(rc, 1)
            self.assertIn("APPROVE", err)
        self.assert_untouched()

    def test_refuses_existing_branch_or_worktree(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.g(self.repo, "branch", "keeper/batch5", "main")
        self.assertEqual(self.run_prep([("build/a", m)])[0], 1)
        self.g(self.repo, "branch", "-d", "keeper/batch5")
        self.wt.mkdir()
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)
        self.assertEqual(self.g(self.repo, "branch", "--list", "keeper/*"), "")

    def test_refuses_to_overwrite_marker_or_brief(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.out.mkdir()
        (self.out / "b5-review-ok").write_text("APPROVE earlier\n")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("would overwrite", err)
        self.assertEqual((self.out / "b5-review-ok").read_text(), "APPROVE earlier\n")
        self.assertFalse(self.wt.exists())

    def test_refuses_when_origin_main_moves(self):
        m = self.branch("build/a", {"a.py": "a\n"})

        def move(*args, **kw):
            self.write(self.repo, "other.txt", "x\n")
            self.g(self.repo, "add", "-A")
            self.g(self.repo, "commit", "-q", "-m", "moved")
            self.g(self.repo, "push", "-q", "origin", "main")
            return set(), []
        with mock.patch.object(lp, "_squash", side_effect=move):
            rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("moved", err)
        self.assertFalse(self.out.exists())

    def test_refuses_conflict_markers_left(self):
        m = self.branch("build/a", {"a.py": "ok\n<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n"})
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("conflict markers", err)
        self.assertFalse(self.out.exists())

    def test_unexpected_conflict_stops(self):
        a = self.branch("build/a", {"x.py": "one\nAAA\nthree\n"})
        b = self.branch("build/b", {"x.py": "one\nBBB\nthree\n"})
        rc, _, err = self.run_prep([("build/a", a), ("build/b", b)])
        self.assertEqual(rc, 1)
        self.assertIn("unexpected conflict", err)
        self.assertIn("x.py", err)
        self.assertFalse(self.out.exists())


class Apply(Prep):
    def test_clean_batch(self):
        a = self.branch("build/a", {"a.py": "a\n"})
        b = self.branch("build/b", {"b.py": "b\n"})
        base = self.g(self.repo, "rev-parse", "origin/main")
        rc, _, err = self.run_prep([("build/a", a), ("build/b", b)])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.g(self.wt, "rev-parse", "--abbrev-ref", "HEAD"), "keeper/batch5")
        self.assertEqual(self.g(self.wt, "log", "--format=%s", f"{base}..HEAD").split("\n"),
                         ["msg for build/b", "msg for build/a"])
        self.assertEqual((self.wt / "b.py").read_text(), "b\n")
        self.assertEqual(self.g(self.wt, "diff", "--stat", "build/a", "HEAD", "--", "a.py"), "")
        self.assertTrue((self.out / "b5-review-ok").read_text().startswith("APPROVE"))
        brief = (self.out / "keeper-brief-b5.md").read_text()
        head8 = self.g(self.wt, "rev-parse", "HEAD")[:8]
        for want in ("batch5", f"HEAD {head8}", f"base main {base[:8]}", "msg for build/a; msg"):
            self.assertIn(want, brief)
        self.assertNotIn("{{", brief)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), base)

    def test_brief_carries_claim_rc_and_plist_rules(self):
        a = self.branch("build/a", {"a.py": "a\n"})
        self.run_prep([("build/a", a)])
        brief = (self.out / "keeper-brief-b5.md").read_text()
        claim = f"{self.repo}/bin/capsule-own {self.repo} keeper/batch5 merge-keeper"
        self.assertLess(brief.index(claim), brief.index("Gates:"))
        for want in ("never pipe a gate to tail", "bin/lib_jobs.sh", "CADENCE", "plist count"):
            self.assertIn(want, brief)

    def test_log_has_one_line_per_step_with_before_sha(self):
        a = self.branch("build/a", {"a.py": "a\n"})
        base = self.g(self.repo, "rev-parse", "origin/main")
        self.run_prep([("build/a", a)])
        rows = [json.loads(ln) for ln in
                (self.tmp / "state" / "landing_prep.jsonl").read_text().splitlines()]
        self.assertEqual([r["what"] for r in rows], ["worktree-add", "squash", "write", "write"])
        self.assertEqual(rows[0]["before"], base)
        self.assertEqual(rows[1]["id"], "build/a")

    def test_second_run_changes_nothing(self):
        a = self.branch("build/a", {"a.py": "a\n"})
        self.run_prep([("build/a", a)])
        head = self.g(self.wt, "rev-parse", "HEAD")
        rc, _, _ = self.run_prep([("build/a", a)])
        self.assertEqual(rc, 1)
        self.assertEqual(self.g(self.wt, "rev-parse", "HEAD"), head)

    def test_intent_conflicts_are_unioned(self):
        def intent_branch(name, new):
            text = INTENT_PY.replace('    "zulu",', f'    "{new}",\n    "zulu",')
            rows = self.tmp / f"{new}.jsonl"
            rows.write_text(f'{{"n": "{new}"}}\n')
            sums = f"{sha(self.repo / 'contracts/intent/alpha.jsonl')}  alpha.jsonl\n" \
                   f"{sha(rows)}  {new}.jsonl\n"
            return self.branch(name, {"capsule/intent.py": text,
                                      "contracts/intent/SHA256SUMS": sums,
                                      f"contracts/intent/{new}.jsonl": rows.read_text()})
        a = intent_branch("build/a", "bravo")
        b = intent_branch("build/b", "charlie")
        rc, _, err = self.run_prep([("build/a", a), ("build/b", b)])
        self.assertEqual((rc, err), (0, ""))
        text = (self.wt / "capsule/intent.py").read_text()
        self.assertIn('    "alpha",\n    "bravo",\n    "charlie",\n    "zulu",\n)', text)
        sums = (self.wt / "contracts/intent/SHA256SUMS").read_text()
        self.assertIn("bravo.jsonl", sums)
        self.assertIn("charlie.jsonl", sums)
        self.assertNotIn("<<<<", sums + text)

    def test_intent_rows_appended_and_repinned(self):
        a = self.branch("build/a", {"a.py": "a\n"})
        rows = self.tmp / "rows.jsonl"
        rows.write_text('{"n": 2}\n')
        imsg = self.tmp / "imsg.txt"
        imsg.write_text("intent: my own message\n\nCo-Authored-By: I <i@i>\n")
        rc, _, err = self.run_prep([("build/a", a)], ["--intent-rows", str(rows), "--intent-file",
                                                      "alpha", "--intent-msg", str(imsg)])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.g(self.wt, "log", "-1", "--format=%B"), imsg.read_text().strip())
        target = self.wt / "contracts/intent/alpha.jsonl"
        self.assertEqual(target.read_text(), '{"n": 1}\n{"n": 2}\n')
        self.assertEqual((self.wt / "contracts/intent/SHA256SUMS").read_text(),
                         f"{sha(target)}  alpha.jsonl\n")

    def test_failed_intent_lint_writes_no_marker_and_logs_refusal(self):
        a = self.branch("build/a", {"a.py": "a\n"})
        rows = self.tmp / "rows.jsonl"
        rows.write_text('{"n": "BAD"}\n')
        imsg = self.tmp / "imsg.txt"
        imsg.write_text("intent\n")
        rc, _, err = self.run_prep([("build/a", a)], ["--intent-rows", str(rows), "--intent-file",
                                                      "alpha", "--intent-msg", str(imsg)])
        self.assertEqual(rc, 1)
        self.assertIn("lint failed", err)
        self.assertFalse(self.out.exists())
        last = (self.tmp / "state" / "landing_prep.jsonl").read_text().splitlines()[-1]
        self.assertEqual(json.loads(last)["what"], "refused")
        self.assertIn("lint failed", json.loads(last)["result"])

    def test_intent_rows_need_intent_msg(self):
        a = self.branch("build/a", {"a.py": "a\n"})
        rows = self.tmp / "rows.jsonl"
        rows.write_text('{"n": 2}\n')
        rc, _, err = self.run_prep([("build/a", a)],
                                   ["--intent-rows", str(rows), "--intent-file", "alpha"])
        self.assertEqual(rc, 1)
        self.assertIn("--intent-msg", err)
        self.assert_untouched()

    def conflicting(self):
        a = self.branch("build/a", {"capsule/__main__.py": "a = 2\n"})
        b = self.branch("build/b", {"capsule/__main__.py": "a = 3\n"})
        return [("build/a", a), ("build/b", b)]

    def test_diff3_user_config_leaves_no_base_section(self):
        env = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "merge.conflictStyle",
               "GIT_CONFIG_VALUE_1": "diff3"}
        with mock.patch.dict(os.environ, env):
            rc, out, err = self.run_prep(self.conflicting())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual((self.wt / "capsule/__main__.py").read_text(), "a = 2\na = 3\n")
        self.assertIn("auto-unioned, unreviewed: capsule/__main__.py", out)
        brief = (self.out / "keeper-brief-b5.md").read_text()
        self.assertIn("auto-unioned, unreviewed: capsule/__main__.py", brief)

    def test_union_that_drops_a_line_is_refused(self):
        with mock.patch.object(lp, "_union", side_effect=lambda t: "a = 2\nx\n"):
            rc, _, err = self.run_prep(self.conflicting())
        self.assertEqual(rc, 1)
        self.assertIn("lost an added line 'a = 3'", err)
        self.assertIn("capsule/__main__.py", err)
        self.assertFalse(self.out.exists())

    def test_marker_lines_of_every_kind_refused(self):
        for n, bad in enumerate(("||||||| base", ">>>>>>> theirs", "=======")):
            m = self.branch(f"build/m{n}", {"a.py": f"ok\n{bad}\n"})
            rc, _, err = self.run_prep([(f"build/m{n}", m)])
            self.assertEqual(rc, 1, bad)
            self.assertIn("conflict markers", err)
            self.g(self.repo, "worktree", "remove", "--force", str(self.wt))
            self.g(self.repo, "update-ref", "-d", "refs/heads/keeper/batch5")

    def test_existing_worktree_refusal_prints_cleanup_commands(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.run_prep([("build/a", m)])
        sha_ = self.g(self.repo, "rev-parse", "keeper/batch5")
        shutil.rmtree(self.out)
        _, _, err = self.run_prep([("build/a", m)])
        self.assertIn(f"git worktree remove {self.wt}", err)
        self.assertIn(f"git update-ref -d refs/heads/keeper/batch5 {sha_}", err)

    def test_preview_fetches_so_the_base_is_current(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        other = self.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.tmp / "o.git"), str(other)])
        self.write(other, "newer.txt", "n\n")
        self.g(other, "add", "-A")
        self.g(other, "commit", "-q", "-m", "newer")
        self.g(other, "push", "-q", "origin", "main")
        newest = self.g(other, "rev-parse", "HEAD")
        _, out, _ = self.run_prep([("build/a", m)], apply=False)
        self.assertIn(f"base origin/main {newest[:8]}", out)

    def _push_from_other(self, name):
        other = self.tmp / ("other-" + name)
        subprocess.run(["git", "clone", "-q", str(self.tmp / "o.git"), str(other)])
        self.write(other, name + ".txt", "n\n")
        self.g(other, "add", "-A")
        self.g(other, "commit", "-q", "-m", name)
        self.g(other, "push", "-q", "origin", "main")
        return self.g(other, "rev-parse", "HEAD")

    def test_apply_fast_forwards_local_main_to_origin(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        newest = self._push_from_other("newer")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), newest)
        self.assertTrue((self.repo / "newer.txt").exists())

    def test_apply_refuses_when_local_main_diverged(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.write(self.repo, "local.txt", "l\n")
        self.g(self.repo, "add", "-A")
        self.g(self.repo, "commit", "-q", "-m", "local only")
        local = self.g(self.repo, "rev-parse", "main")
        self._push_from_other("newer")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("diverged from origin/main", err)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), local)
        self.assert_untouched()

    def test_apply_leaves_local_main_ahead_of_origin_alone(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.write(self.repo, "local.txt", "l\n")
        self.g(self.repo, "add", "-A")
        self.g(self.repo, "commit", "-q", "-m", "local only")
        local = self.g(self.repo, "rev-parse", "main")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), local)

    def test_preview_does_not_move_local_main(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        before = self.g(self.repo, "rev-parse", "main")
        self._push_from_other("newer")
        self.run_prep([("build/a", m)], apply=False)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), before)


class SyncMain(Prep):
    """Row 643 follow-up: a factory landing pushes origin only, so the packer brings local main
    up to origin/main itself (fast-forward, clean checkout) or refuses naming the fix."""
    def advance_origin(self, name="newer.txt"):
        other = self.tmp / "other"
        if not other.exists():
            subprocess.run(["git", "clone", "-q", str(self.tmp / "o.git"), str(other)])
        self.write(other, name, "n\n")
        self.g(other, "add", "-A")
        self.g(other, "commit", "-q", "-m", name)
        self.g(other, "push", "-q", "origin", "main")
        return self.g(other, "rev-parse", "HEAD")

    def test_apply_fast_forwards_a_behind_main(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        newest = self.advance_origin()
        rc, out, err = self.run_prep([("build/a", m)])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), newest)
        self.assertTrue((self.repo / "newer.txt").exists())
        self.assertIn(f"fast-forwarded main to {newest[:8]}", out)

    def test_preview_says_it_would_and_changes_nothing(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        old = self.g(self.repo, "rev-parse", "main")
        newest = self.advance_origin()
        rc, out, _ = self.run_prep([("build/a", m)], apply=False)
        self.assertEqual(rc, 0)
        self.assertIn(f"would fast-forward main {old[:8]} -> {newest[:8]}", out)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), old)

    def test_up_to_date_main_is_left_alone(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        rc, out, _ = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 0)
        self.assertNotIn("fast-forward", out)

    def test_dirty_checkout_is_refused_naming_the_fix(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        old = self.g(self.repo, "rev-parse", "main")
        self.advance_origin()
        self.write(self.repo, "x.py", "changed\n")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("local main is behind origin/main", err)
        self.assertIn("uncommitted changes", err)
        self.assertIn(f"git -C {self.repo} status", err)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), old)
        self.assert_untouched()

    def test_diverged_main_is_refused_naming_the_fix(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.write(self.repo, "local.txt", "l\n")
        self.g(self.repo, "add", "-A")
        self.g(self.repo, "commit", "-q", "-m", "local only")
        local = self.g(self.repo, "rev-parse", "main")
        self.advance_origin()
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("diverged from origin/main", err)
        self.assertIn("by hand (not run)", err)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), local)
        self.assert_untouched()

    def test_preview_of_a_dirty_checkout_names_the_refusal(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.advance_origin()
        self.write(self.repo, "x.py", "changed\n")
        rc, out, _ = self.run_prep([("build/a", m)], apply=False)
        self.assertEqual(rc, 0)
        self.assertIn("apply would refuse", out)
        self.assertIn("uncommitted changes", out)

    def test_untracked_files_do_not_block_the_fast_forward(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        newest = self.advance_origin()
        self.write(self.repo, "scratch.txt", "s\n")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), newest)

    def test_behind_on_another_branch_is_refused(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.advance_origin()
        self.g(self.repo, "switch", "-q", "build/a")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("is on branch build/a", err)
        self.assertIn(f"git -C {self.repo} switch main", err)

    def test_detached_head_is_refused(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.advance_origin()
        self.g(self.repo, "switch", "-q", "--detach")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("is on no branch", err)

    def test_main_in_a_linked_worktree_is_named(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.advance_origin()
        self.g(self.repo, "switch", "-q", "build/a")
        linked = self.tmp / "linked-main"
        self.g(self.repo, "worktree", "add", "-q", str(linked), "main")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn(f"main is checked out in {linked.resolve()}", err)
        self.assertNotIn("switch main", err)

    def test_no_local_main_has_a_clear_refusal(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        self.g(self.repo, "switch", "-q", "build/a")
        self.g(self.repo, "branch", "-q", "-D", "main")
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("there is no local main branch", err)
        self.assertNotIn("rev-parse failed", err)

    def test_apply_rechecks_the_branch_before_merging(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        newest = self.advance_origin()
        real = lp._sync_main

        def then_switch(a, base):
            out = real(a, base)
            self.g(self.repo, "switch", "-q", "build/a")      # the checkout moves after _plan
            return out
        with mock.patch.object(lp, "_sync_main", then_switch):
            rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("is on branch build/a", err)
        self.assertIn("main not fast-forwarded", err)
        self.assertNotEqual(self.g(self.repo, "rev-parse", "main"), newest)
        self.assertFalse(self.out.exists())

    def test_apply_rechecks_for_a_dirty_tracked_file_before_merging(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        old = self.g(self.repo, "rev-parse", "main")
        self.advance_origin()
        real = lp._sync_main

        def then_dirty(a, base):
            out = real(a, base)
            self.write(self.repo, "x.py", "dirtied after plan\n")
            return out
        with mock.patch.object(lp, "_sync_main", then_dirty):
            rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("uncommitted changes", err)
        self.assertIn("main not fast-forwarded", err)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), old)
        self.assertFalse(self.out.exists())

    def test_a_later_refusal_leaves_main_unmoved(self):
        m = self.branch("build/a", {"a.py": "ok\n<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n"})
        old = self.g(self.repo, "rev-parse", "main")
        self.advance_origin()
        rc, out, err = self.run_prep([("build/a", m)])
        self.assertEqual(rc, 1)
        self.assertIn("conflict markers", err)
        self.assertEqual(self.g(self.repo, "rev-parse", "main"), old)
        self.assertNotIn("fast-forwarded", out)


class Brief(Prep):
    def brief(self):
        m = self.branch("build/a", {"a.py": "a\n"})
        rc, _, err = self.run_prep([("build/a", m)])
        self.assertEqual((rc, err), (0, ""))
        return (self.out / "keeper-brief-b5.md").read_text()

    def test_brief_never_tells_the_keeper_to_unset_the_no_send_guard(self):
        brief = self.brief()
        self.assertNotIn("AGENT_OPS_NO_SEND", brief)
        self.assertNotIn("env -u", brief)
        self.assertNotIn("unset", brief)

    def test_brief_says_a_hook_or_classifier_refusal_is_final(self):
        brief = self.brief()
        self.assertEqual(brief.count("refused by a hook or classifier is final"), 1)
        self.assertIn("stop and report", brief)


class Checkout(Prep):
    def test_main_checkout_from_a_linked_worktree(self):
        linked = self.tmp / "linked"
        self.g(self.repo, "worktree", "add", "-q", "--detach", str(linked), "main")
        self.assertEqual(lp._main_checkout(linked), str(self.repo.resolve()))
        self.assertEqual(lp._main_checkout(self.repo), str(self.repo.resolve()))

    def test_main_checkout_refuses_outside_a_repo(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaises(lp.Refuse):
            lp._main_checkout(plain)


class Union(unittest.TestCase):
    def test_only_an_exact_separator_line_splits_sides(self):
        text = "<<<<<<< HEAD\n=========== title\nours\n=======\ntheirs\n>>>>>>> b\n"
        self.assertEqual(lp._union(text), "=========== title\nours\ntheirs\n")

    def test_crlf_separator_still_splits(self):
        text = "<<<<<<< HEAD\r\nours\r\n=======\r\ntheirs\r\n>>>>>>> b\r\n"
        self.assertEqual(lp._union(text), "ours\r\ntheirs\r\n")

    def test_union_keeps_both_sides_ours_first(self):
        text = "a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> br\nz\n"
        self.assertEqual(lp._union(text), "a\nours\ntheirs\nz\n")

    def test_conflict_outside_standalone_refused(self):
        text = 'STANDALONE = (\n    "a",\n)\nx\n<<<<<<< HEAD\n1\n=======\n2\n>>>>>>> b\n'
        with self.assertRaises(lp.Refuse):
            lp._union_intent_py(text)


if __name__ == "__main__":
    unittest.main()
