"""maintenance/branch_sweep.py on throwaway git repos (origin + clone named capsule-corp), a temp
CAPSULE_HOME with fake boards and a temp state dir. Oracle 1: the refs, folders and log read back
with plain git after the run. Oracle 2: a hand-written list of what must remain. Never the live
repo, boards or state."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import branch_sweep as bs  # noqa: E402

IDENT = ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false")


def g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *IDENT, *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bsweep-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo, self.home, self.state = (self.root / "capsule-corp", self.root / "home",
                                            self.root / "state")
        g(self.root, "init", "-q", "-b", "main", str(self.repo))
        g(self.root, "init", "-q", "--bare", "-b", "main", str(self.root / "origin.git"))
        self.commit(self.repo, "base.txt", "base")
        g(self.repo, "remote", "add", "origin", str(self.root / "origin.git"))
        g(self.repo, "push", "-q", "origin", "main")
        g(self.repo, "fetch", "-q", "origin")
        env = mock.patch.dict(os.environ, {"CAPSULE_HOME": str(self.home),
                                           "AGENT_OPS_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        self.real_cwds = bs._cwds
        for patch in (mock.patch.object(bs, "FRESH_S", 0),     # nothing is fresh unless a test says
                      mock.patch.object(bs, "_cwds", return_value=set())):
            patch.start()
            self.addCleanup(patch.stop)
        self.board()

    def commit(self, where, name, text):
        (Path(where) / name).write_text(text)
        g(where, "add", name)
        g(where, "commit", "-q", "-m", f"add {name}")
        return g(where, "rev-parse", "HEAD")

    def branch(self, name, file="x.txt", text="x", folder=True):
        """A branch with one commit on origin/main, in its own capsule-corp-<slug> folder."""
        wt = self.root / f"capsule-corp-{name.replace('/', '-')}"
        g(self.repo, "worktree", "add", "-q", "-b", name, str(wt), "origin/main")
        self.commit(wt, file, text)
        if not folder:
            g(self.repo, "worktree", "remove", str(wt))
        return wt

    def land(self, file="x.txt", text="x"):
        """The same change reaches origin/main as a squash would: a different commit."""
        self.commit(self.repo, file, text)
        g(self.repo, "push", "-q", "origin", "main")
        g(self.repo, "fetch", "-q", "origin")

    def run_sweep(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bs.main([*flags, "--repo", str(self.repo)])
        return rc, out.getvalue(), err.getvalue()

    def branches(self):
        return set(g(self.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    def owner(self, *names):
        d = self.home / "queue" / "capsule-corp"
        d.mkdir(parents=True)
        rows = {n: {"owner": "x", "heartbeat": 1} for n in names}
        (d / "owners.json").write_text(json.dumps(rows))

    def board(self, branch=None, epic=None):
        d = self.home / "boards" / "b1"
        d.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(d / "kanban.db")
        con.execute("create table if not exists tasks (id text, branch_name text)")
        con.execute("create table if not exists task_events (task_id text, kind text, "
                    "payload text)")
        con.execute("insert into tasks values ('t1', ?)", (branch,))
        con.execute("insert into task_events values ('t1', 'epic_assigned', ?)",
                    (json.dumps({"branch": epic}),))
        con.commit()
        con.close()

class Sweep(Base):
    def test_landed_removed_unlanded_and_protected_kept(self):
        landed, squash = self.branch("keeper/landed", "a.txt", "a"), self.branch("build/sq")
        self.branch("build/new", "n.txt", "n")
        for name in ("build/owned", "build/card", "build/epicbranch", "build/salvage-1"):
            self.branch(name, f"{name[6:]}.txt", name)
        g(self.repo, "merge", "-q", "--ff-only", "keeper/landed")
        g(self.repo, "push", "-q", "origin", "main")
        g(self.repo, "fetch", "-q", "origin")
        self.land("x.txt", "x")                  # build/sq is landed by a squash, not ancestry
        for name in ("owned", "card", "epicbranch", "salvage-1"):
            self.land(f"{name}.txt", f"build/{name}")
        self.owner("build/owned")
        self.board(branch="build/card", epic="build/epicbranch")
        before = self.branches()
        rc, out, _ = self.run_sweep()
        self.assertEqual((rc, self.branches()), (0, before))        # preview changes nothing
        self.assertTrue(landed.exists() and not (self.state / bs.LOG).exists())
        self.assertIn("would remove branch build/sq", out)
        rc, out, _ = self.run_sweep("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.branches(), before - {"keeper/landed", "build/sq"})
        self.assertFalse(landed.exists() or squash.exists())
        rows = [json.loads(ln) for ln in (self.state / bs.LOG).read_text().splitlines()]
        self.assertEqual({(r["what"], r["id"], r["result"]) for r in rows},
                         {(w, i, s) for w, i in [("branch", "keeper/landed"),
                                                 ("branch", "build/sq"),
                                                 ("worktree", str(landed)),
                                                 ("worktree", str(squash))]
                          for s in ("starting", "removed")})        # starting row, then result
        self.assertEqual({r["branch"] for r in rows if r["what"] == "worktree"},
                         {"keeper/landed", "build/sq"})
        self.assertEqual(self.run_sweep("--apply")[0], 0)           # second run: nothing to do
        self.assertEqual(len((self.state / bs.LOG).read_text().splitlines()), 8)
        g(self.repo, "branch", "keeper/landed", next(r["before"] for r in rows
                                                     if r["id"] == "keeper/landed"))
        self.assertIn("keeper/landed", self.branches())             # the log restores it

    def test_dirty_locked_outside_stale_and_checked_out_are_kept(self):
        names = ("dirty", "locked", "stale", "outside", "main-co")
        dirty, locked, stale, outside, _ = (self.branch(f"build/{n}", f"{n}.txt", n)
                                            for n in names)
        g(self.repo, "worktree", "remove", str(_))
        for n in names:                      # every branch is landed, so only the folder rules keep
            self.land(f"{n}.txt", n)
        (dirty / "new.txt").write_text("untracked")
        g(self.repo, "worktree", "lock", str(locked))
        shutil.rmtree(stale)
        far = self.root / "elsewhere"
        g(self.repo, "worktree", "move", str(outside), str(far))
        g(self.repo, "checkout", "-q", "build/main-co")
        before = self.branches()
        rc, out, _ = self.run_sweep("--apply")
        self.assertEqual((rc, self.branches()), (0, before))
        self.assertTrue(dirty.exists() and locked.exists() and far.exists())
        for needle in ("has changes", "locked", "stale entry", "not under capsule-corp-*",
                       "checked out in"):
            self.assertIn(needle, out)
        self.assertIn(str(stale), g(self.repo, "worktree", "list"))   # prune is never run

    def test_detached_clean_folder_removed_and_apply_fetches(self):
        wt = self.root / "capsule-corp-det"
        g(self.repo, "worktree", "add", "-q", "--detach", str(wt), "origin/main")
        self.branch("build/late", "late.txt", "late")
        rc, out, _ = self.run_sweep()                  # the dry run uses the current origin/main
        self.assertIn("kept: branch build/late (not landed)", out)
        other = self.root / "other"
        g(self.root, "clone", "-q", str(self.root / "origin.git"), str(other))
        self.commit(other, "late.txt", "late")
        g(other, "push", "-q", "origin", "main")
        self.assertEqual(self.run_sweep("--apply")[0], 0)          # --apply fetches first
        self.assertNotIn("build/late", self.branches())
        self.assertFalse(wt.exists())

    def test_moved_branch_is_not_deleted_and_failure_exits_nonzero(self):
        self.branch("build/moved", folder=False)
        sha = g(self.repo, "rev-parse", "build/moved")
        g(self.repo, "update-ref", "refs/heads/build/moved", "origin/main")  # moved after check
        res = bs._do(self.repo, {"what": "branch", "id": "build/moved", "branch": "build/moved",
                                 "before": sha})
        self.assertTrue(res.startswith("failed"), res)
        self.assertIn("build/moved", self.branches())

    def kept_after_apply(self, *names):
        before = self.branches()
        rc, out, _ = self.run_sweep("--apply")
        self.assertEqual((rc, self.branches()), (0, before), out)
        for n in names:
            self.assertIn(f"kept: branch {n}", out)
        return out

    def test_conflict_and_reverted_change_are_not_landed(self):
        self.branch("build/conflict", "base.txt", "branch version")
        self.branch("build/reverted", "y.txt", "y")
        self.land("base.txt", "main version")
        self.land("y.txt", "y")
        g(self.repo, "rm", "-q", "y.txt")
        g(self.repo, "commit", "-q", "-m", "revert y")
        g(self.repo, "push", "-q", "origin", "main")
        g(self.repo, "fetch", "-q", "origin")
        self.kept_after_apply("build/conflict", "build/reverted")

    def test_fresh_work_is_kept(self):
        wt = self.branch("build/fresh")
        self.land()
        with mock.patch.object(bs, "FRESH_S", 24 * 3600):
            out = self.kept_after_apply()
        self.assertIn("kept: branch build/fresh (touched under 24h ago)", out)
        self.assertIn(f"kept: worktree {wt} (touched under 24h ago)", out)
        self.assertTrue(wt.exists())
        self.assertIn("would remove branch build/fresh", self.run_sweep()[1])   # old enough: goes

    def test_no_lsof_keeps_every_folder_and_never_crashes(self):
        wt = self.branch("build/unchecked")
        self.land()
        empty = self.root / "emptybin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertIsNone(self.real_cwds())                     # no lsof on PATH
        with mock.patch.object(bs, "_cwds", return_value=None):         # what the plan then sees
            out = self.kept_after_apply("build/unchecked")
        self.assertIn(f"kept: worktree {wt} (lsof not available: kept)", out)
        self.assertTrue(wt.exists())

    def test_an_lsof_killed_mid_output_keeps_every_folder(self):
        wt = self.branch("build/halfread")
        self.land()
        fake = self.root / "fakebin"
        fake.mkdir()
        (fake / "lsof").write_text("#!/bin/sh\nprintf 'n/elsewhere\\n'\nkill -9 $$\n")
        (fake / "lsof").chmod(0o755)
        path = {"PATH": f"{fake}:{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, path):
            self.assertIsNone(self.real_cwds())             # a signal, not rc 0 or 1: unchecked
        with mock.patch.object(bs, "_cwds", side_effect=self.real_cwds), \
                mock.patch.dict(os.environ, path):
            out = self.kept_after_apply("build/halfread")
        self.assertIn(f"kept: worktree {wt} (lsof not available: kept)", out)
        self.assertTrue(wt.exists())

    def test_folder_a_fake_lsof_reports_stays_without_a_real_lsof(self):
        wt = self.branch("build/fakebusy")
        self.land()
        fake = self.root / "fakebin"
        fake.mkdir()
        (fake / "lsof").write_text(f"#!/bin/sh\nprintf 'n/elsewhere\\nn{wt}\\n'\nexit 1\n")
        (fake / "lsof").chmod(0o755)
        path = {"PATH": f"{fake}:{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, path):
            self.assertIn(str(wt), self.real_cwds())                # rc 1 with output: parsed
        with mock.patch.object(bs, "_cwds", side_effect=self.real_cwds), \
                mock.patch.dict(os.environ, path):
            out = self.kept_after_apply("build/fakebusy")
        self.assertIn(f"kept: worktree {wt} (a process is working in it)", out)
        self.assertTrue(wt.exists())

    @unittest.skipUnless(shutil.which("lsof"), "needs lsof")
    def test_folder_a_process_is_in_stays(self):
        wt = self.branch("build/busy")
        self.land()
        proc = subprocess.Popen(["sleep", "60"], cwd=wt)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertIn(str(wt), self.real_cwds())                    # the real lsof parse
        with mock.patch.object(bs, "_cwds", side_effect=self.real_cwds):
            out = self.kept_after_apply("build/busy")
        self.assertIn(f"kept: worktree {wt} (a process is working in it)", out)
        with mock.patch.object(bs.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1, "", "")):
            with self.assertRaises(RuntimeError):                   # lsof failing refuses
                self.real_cwds()

    def test_zero_boards_refuses(self):
        self.branch("build/a", folder=False)
        self.land()
        shutil.rmtree(self.home / "boards")
        rc, _, err = self.run_sweep("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("no kanban.db", err)
        self.assertIn("build/a", self.branches())

    def test_failed_folder_removal_skips_its_branch(self):
        wt = self.branch("build/stuck")
        self.land()
        self.state.mkdir()
        self.root.chmod(0o555)              # git cannot delete a folder in a read-only parent
        self.addCleanup(self.root.chmod, 0o755)
        rc, out, _ = self.run_sweep("--apply")
        self.assertEqual(rc, 1, out)
        self.assertIn("build/stuck", self.branches())
        self.assertIn("skipped: its work folder was not removed: branch build/stuck", out)
        self.assertTrue(wt.exists())

    def test_head_moved_after_planning_is_kept(self):
        wt = self.branch("build/moving", folder=True)
        planned = g(wt, "rev-parse", "HEAD")
        self.commit(wt, "later.txt", "later")            # the folder moved on after the check
        res = bs._do(self.repo, self.planned(wt, "build/moving", planned))
        self.assertTrue(res.startswith("kept"), res)
        self.assertTrue(wt.exists())

    def planned(self, wt, name, sha=None):
        return {"what": "worktree", "id": str(wt), "branch": name,
                "before": sha or g(wt, "rev-parse", "HEAD")}

    def test_recheck_finds_a_dirty_tree_or_a_new_claim(self):
        wt = self.branch("build/late-edit")
        item = self.planned(wt, "build/late-edit")
        (wt / "scratch.txt").write_text("untracked since the check")
        self.assertIn("changed since the check", bs._do(self.repo, item))
        (wt / "scratch.txt").unlink()
        self.board(branch="build/late-edit")                  # a card claims it after the plan
        self.assertEqual(bs._do(self.repo, item), "kept: claimed since the check")
        self.assertTrue(wt.exists() and "build/late-edit" in self.branches())
        rows = {"build/late-edit": {"owner": "x", "heartbeat": 1}}      # an owners entry too
        (self.home / "queue" / "capsule-corp").mkdir(parents=True)
        (self.home / "queue" / "capsule-corp" / "owners.json").write_text(json.dumps(rows))
        branch = {"what": "branch", "id": "build/late-edit", "branch": "build/late-edit",
                  "before": item["before"]}
        self.assertEqual(bs._do(self.repo, branch), "kept: claimed since the check")

    def test_ignored_files_keep_a_folder_but_caches_do_not(self):
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_text(".env\n__pycache__/\n")
        secret, cache = self.branch("build/secret"), self.branch("build/cache")
        self.land()
        (secret / ".env").write_text("TOKEN=1")
        (cache / "pkg" / "__pycache__").mkdir(parents=True)
        (cache / "pkg" / "__pycache__" / "m.cpython-314.pyc").write_text("x")
        rc, out, _ = self.run_sweep("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"kept: worktree {secret} (ignored files present: .env)", out)
        self.assertTrue(secret.exists() and "build/secret" in self.branches())
        self.assertFalse(cache.exists() or "build/cache" in self.branches())

    def test_untracked_file_is_seen_even_when_git_is_set_to_hide_it(self):
        wt = self.branch("build/hidden")
        self.land()
        g(self.repo, "config", "status.showUntrackedFiles", "no")
        (wt / "new.txt").write_text("untracked")
        self.assertEqual(g(wt, "status", "--porcelain"), "")       # the setting does hide it
        out = self.kept_after_apply("build/hidden")
        self.assertIn(f"kept: worktree {wt} (has changes)", out)
        self.assertTrue(wt.exists())

    def test_symlink_and_dotdot_paths_are_kept(self):
        outside = self.root / "outside" / "capsule-corp-real"
        outside.mkdir(parents=True)
        link = self.root / "capsule-corp-link"
        link.symlink_to(outside)
        for path in (link, self.root / "capsule-corp-a" / ".." / "outside",
                     self.root / "outside" / ".." / "capsule-corp" / "sub"):
            wt = {"worktree": str(path), "HEAD": "0" * 40}
            self.assertEqual(bs._wt_static(self.repo, wt, set()), "not under capsule-corp-*")
        inside = {"worktree": str(self.root / "capsule-corp-ok"), "HEAD": "0" * 40}
        self.assertIsNone(bs._wt_static(self.repo, inside, set()))

    def test_refusals(self):
        self.board()
        (self.home / "boards" / "b1" / "kanban.db").write_text("not a database")
        self.branch("build/a", folder=False)
        self.land()
        rc, _, err = self.run_sweep("--apply")
        self.assertEqual(rc, 1)
        self.assertIn("refused", err)
        self.assertIn("build/a", self.branches())
        shutil.rmtree(self.home / "boards")
        g(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(self.run_sweep()[0], 1)


class IntentRows(Base):
    """Z1 (row 643): contracts/intent/branch_sweep.jsonl, replayed once the CoS has stamped it
    (the storage_gc pattern). The built-in cases run with or without the file."""
    CASES = {"squash-landed-branch-removed-and-logged": "squash",
             "ignored-env-or-fresh-work-kept": "env",
             "card-named-or-unlanded-branch-kept": "card"}
    WANT = {"squash": {"exit": 0, "branch_removed": 1, "folder_removed": 1,
                       "log_has_name_and_sha": 1},
            "env": {"exit": 0, "env_folder_removed": 0, "env_branch_removed": 0,
                    "fresh_branch_removed": 0},
            "card": {"exit": 0, "card_branch_removed": 0, "unlanded_branch_removed": 0,
                     "unlanded_folder_removed": 0}}

    def replay(self, case):
        if case == "squash":
            wt = self.branch("build/sq")
            sha = g(self.repo, "rev-parse", "build/sq")
            self.land()                         # the same change arrives as a squash commit
            rc = self.run_sweep("--apply")[0]
            rows = [json.loads(ln) for ln in (self.state / bs.LOG).read_text().splitlines()]
            return {"exit": rc, "branch_removed": int("build/sq" not in self.branches()),
                    "folder_removed": int(not wt.exists()),
                    "log_has_name_and_sha": int(any(
                        r["id"] == "build/sq" and r["before"] == sha and r["result"] == "removed"
                        for r in rows))}
        if case == "env":
            exclude = self.repo / ".git" / "info" / "exclude"
            exclude.parent.mkdir(exist_ok=True)
            exclude.write_text(".env\n")
            secret = self.branch("build/secret", "s.txt", "s")
            self.land("s.txt", "s")
            (secret / ".env").write_text("TOKEN=1")
            rc = self.run_sweep("--apply")[0]
            self.branch("build/fresh", "f.txt", "f")
            self.land("f.txt", "f")
            with mock.patch.object(bs, "FRESH_S", 24 * 3600):
                rc = rc or self.run_sweep("--apply")[0]
            return {"exit": rc, "env_folder_removed": int(not secret.exists()),
                    "env_branch_removed": int("build/secret" not in self.branches()),
                    "fresh_branch_removed": int("build/fresh" not in self.branches())}
        self.branch("build/card", "c.txt", "c")
        self.land("c.txt", "c")
        self.board(branch="build/card")
        new = self.branch("build/new", "n.txt", "n")
        rc = self.run_sweep("--apply")[0]
        return {"exit": rc, "card_branch_removed": int("build/card" not in self.branches()),
                "unlanded_branch_removed": int("build/new" not in self.branches()),
                "unlanded_folder_removed": int(not new.exists())}

    def test_the_built_in_cases_hold_without_the_file(self):
        for case, want in self.WANT.items():
            with self.subTest(case=case):
                self.setUp()                    # a fresh temp repo per case
                self.assertEqual(self.replay(case), want)

    def test_the_intent_rows_hold_when_the_file_exists(self):
        from capsule import intent
        rows = intent.load("branch_sweep")
        self.assertEqual({r["id"] for r in rows}, set(self.CASES))
        for row in rows:
            self.setUp()
            got = self.replay(self.CASES[row["id"]])
            for key, want in row["expect"].items():
                if isinstance(want, int):
                    self.assertEqual(got[key], want, row["id"])


if __name__ == "__main__":
    unittest.main()
