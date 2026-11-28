# mutation-check: targets-predate-range ff625e94 -- comment-only edit, no behaviour changed ::
#   CloneFailureRedaction.test_a_failing_clone_with_a_token_in_the_url_never_leaks_it
"""The implementer pre-edit worktree check (row 465, t_685fd87b; review I2): `workspace.is_worktree`."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db as capsule_db
from capsule.tests import tmpclean
from capsule import paths, preflight, workspace
from capsule.tests import wal_oracle


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


class IsWorktree(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.origin = self.dir / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "README.md").write_text("x\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.root = self.dir / "root"

    def test_a_clone_from_workspace_clone_is_its_own_worktree(self):
        ws = workspace.clone(str(self.origin), "main", self.root)
        self.assertTrue(workspace.is_worktree(ws, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_a_path_that_does_not_exist_is_refused(self):
        self.assertFalse(workspace.is_worktree(self.root / "never-cloned", self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_a_plain_directory_with_no_git_dir_is_refused(self):
        """The exact incident shape (t_685fd87b): the assigned workspace path is some other real
        directory (main's own checkout, say) rather than the clone the tick made for this task."""
        somewhere_else = self.dir / "not-the-workspace"
        somewhere_else.mkdir()
        self.assertFalse(workspace.is_worktree(somewhere_else, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_the_primary_checkout_is_refused(self):
        """Main's own checkout: a real git repo, just never cloned under this epic's `root` and
        never given a separate git dir there (review I2's "main checkout" probe)."""
        main_checkout = self.dir / "main-checkout"
        main_checkout.mkdir()
        git(main_checkout, "init", "-q")
        self.assertFalse(workspace.is_worktree(main_checkout, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_an_ordinary_git_repo_that_is_not_this_clone_is_refused(self):
        """A repo that happens to sit directly under `root`, with its own (non-separate) `.git`:
        refused on the git-dir-identity check even though the parent-of-root guard alone would
        let it through."""
        other = self.root / "unrelated-repo"
        other.mkdir(parents=True)
        git(other, "init", "-q")
        self.assertFalse(workspace.is_worktree(other, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_a_subdirectory_of_the_clone_is_refused(self):
        """`ws.resolve().parent != root` for a subdirectory of the clone (it sits one level too
        deep), regardless of what `rev-parse` says from there."""
        ws = workspace.clone(str(self.origin), "main", self.root)
        sub = Path(ws) / "sub"
        sub.mkdir()
        self.assertFalse(workspace.is_worktree(sub, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_a_symlinked_workspace_is_refused(self):
        ws = workspace.clone(str(self.origin), "main", self.root)
        link = self.root / "link-to-ws"
        link.symlink_to(ws)
        self.assertFalse(workspace.is_worktree(link, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_a_correct_clone_under_a_different_root_is_refused(self):
        """Round-3 fix, R2: an otherwise-genuine clone of this same repo (right git dir, right
        origin) but made under a different root than the one this check is given - only the
        parent-of-root guard refuses it; every other guard would pass."""
        other_root = self.dir / "other-root"
        ws = workspace.clone(str(self.origin), "main", other_root)
        self.assertFalse(workspace.is_worktree(ws, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_a_symlink_whose_gitdir_also_exists_is_refused_only_by_the_symlink_guard(self):
        """Round-3 fix, R2: `root/link` symlinked to a real clone `ws`, plus a `.gitdirs/link`
        symlink pointing at `ws`'s own real git dir (`gitdir(link)` resolves to `gitdir(ws)`) -
        so the parent-of-root, git-dir-exists and origin checks would all pass from `link`. Only
        `ws.is_symlink()` refuses this; drop that guard and it is accepted."""
        ws = workspace.clone(str(self.origin), "main", self.root)
        link = self.root / "link"
        link.symlink_to(ws)
        gd_link = workspace.gitdir(link)
        gd_link.parent.mkdir(parents=True, exist_ok=True)
        gd_link.symlink_to(workspace.gitdir(ws))
        self.assertFalse(workspace.is_worktree(link, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_missing_root_or_repo_url_is_refused_not_a_crash(self):
        """Round-3 fix, R2: a job.json predating work_root/repo_url (or one corrupted to drop
        them) must refuse cleanly, never raise (a bare `Path(None)` would be a TypeError, which
        would crash `worker.run_one` instead of emitting a typed `worktree_mismatch`)."""
        ws = workspace.clone(str(self.origin), "main", self.root)
        self.assertFalse(workspace.is_worktree(ws, None, workspace.repo_url_sha256(str(self.origin))))
        self.assertFalse(workspace.is_worktree(ws, self.root, None))

    def test_a_crafted_bare_repo_under_gitdirs_is_refused(self):
        """review I2's first probe: a directory under `root` plus a bare repo planted at the exact
        path `gitdir()` would compute for it - `is_dir()` on the git dir alone must not be enough."""
        evil = self.root / "evil"
        evil.mkdir(parents=True)
        gd = workspace.gitdir(evil)
        gd.parent.mkdir(parents=True, exist_ok=True)
        git(gd.parent, "init", "-q", "--bare", gd.name)
        self.assertFalse(workspace.is_worktree(evil, self.root, workspace.repo_url_sha256(str(self.origin))))

    def test_a_foreign_repos_git_dir_moved_under_gitdirs_is_refused(self):
        """review I2's second probe: a real (non-bare) repo's `.git` directory relocated to sit at
        `gitdir(ws)`, with a `ws/.git` pointer file aimed at it, exactly as `_repoint` would leave
        a genuine clone - but this repo's `remote.origin.url` is not this epic's `repo_url`."""
        foreign = self.dir / "foreign-origin"
        foreign.mkdir()
        git(foreign, "init", "-q", "-b", "main")
        (foreign / "f.txt").write_text("y\n")
        git(foreign, "add", ".")
        git(foreign, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        foreign_ws = self.dir / "foreign-clone"
        git(self.dir, "clone", "-q", str(foreign), str(foreign_ws))

        pretend = self.root / "pretend"
        pretend.mkdir(parents=True)
        gd = workspace.gitdir(pretend)
        gd.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(foreign_ws / ".git"), str(gd))
        for item in foreign_ws.iterdir():
            shutil.move(str(item), str(pretend / item.name))
        (pretend / ".git").write_text(f"gitdir: {gd}\n")
        self.assertFalse(workspace.is_worktree(pretend, self.root, workspace.repo_url_sha256(str(self.origin))))


class FenceFilesProtected(unittest.TestCase):
    """the owner 2026-10-18 (t_d4f17054): workspace.py holds the protection list and its matcher,
    lander.py the lander's checks; a worker diff touching either is flagged, as for intent.py."""

    def test_a_worker_write_to_workspace_py_is_refused(self):
        self.assertEqual(workspace.touches_protected(["capsule/workspace.py", "capsule/tick.py"]),
                         ["capsule/workspace.py"])

    def test_a_worker_write_to_lander_py_is_refused_though_the_file_may_not_exist(self):
        self.assertEqual(workspace.touches_protected(["capsule/lander.py", "capsule/tick.py"]),
                         ["capsule/lander.py"])


JUDGE_PATHS = ["docker/base/Dockerfile", "docker/stacks/python/Dockerfile", "docker/IMAGES.lock",
               "capsule/image_render.py", "capsule/blind/harness/run.py",
               "capsule/blind/harness/nested/deep.py", "maintenance/meters.py",
               "calibration/planted/bug-01.diff", "capsule/interrupts.json",
               "calibration", "docker/stacks", "capsule/blind",   # bare roots: a committed symlink
               "docker/build-base.sh", "docker/BUILD_MIN_FREE_BYTES"]


class JudgePathsProtected(unittest.TestCase):
    """R02: every judge path sits outside the worker-writable set, even before it exists."""

    def test_each_judge_path_is_flagged_and_unrelated_paths_are_not(self):
        unrelated = ["capsule/tick.py", "docker/README.md",
                     "maintenance/other.py", "capsule/blind/other.py", "calibration.md",
                     "capsule/blindness.py"]
        self.assertEqual(workspace.touches_protected(JUDGE_PATHS + unrelated), JUDGE_PATHS)

    def test_the_retired_image_pin_is_not_in_the_judge_list(self):
        self.assertNotIn("docker/WORKER_IMAGE", workspace.PROTECTED)

    def test_every_shipped_intent_row_holds(self):
        for line in (paths.INTENT / "protected_paths.jsonl").read_text().splitlines():
            row = json.loads(line)
            self.assertEqual(workspace.touches_protected(row["given"]["document"]["paths"]),
                             row["expect"]["flagged"], row["id"])

    def test_a_planted_stack_dockerfile_is_flagged_and_restored(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, tmp)
        origin = tmp / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        (origin / "README.md").write_text("x\n")
        git(origin, "add", ".")
        git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        ws = workspace.clone(str(origin), "main", tmp / "root")
        base = "HEAD"
        planted = ws / "docker" / "stacks" / "python" / "Dockerfile"
        planted.parent.mkdir(parents=True)
        planted.write_text("FROM scratch\n")
        touched = workspace.changed(ws, base)
        self.assertEqual(touched, ["docker/stacks/python/Dockerfile"])
        flagged = workspace.touches_protected(touched)
        self.assertEqual(flagged, ["docker/stacks/python/Dockerfile"])
        workspace.restore(ws, base, flagged)      # the call Tick._verdict makes on a hit
        self.assertFalse(planted.exists())
        self.assertEqual(workspace.changed(ws, base), [])


class OriginNormalisation(unittest.TestCase):
    """Round-3 fix, R4: `git clone` stores a relative local `repo_url` as an absolute path (its
    own resolution, against the process cwd, at clone time), and a trailing `.git`/`/` on a URL
    is cosmetic. `repo_url_sha256` must normalise before hashing, on both sides, or a relative
    `repo_url` in config.json would never match its own clone's origin."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.origin = self.dir / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "README.md").write_text("x\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.root = self.dir / "root"

    def test_a_relative_repo_url_matches_the_absolute_origin_git_actually_stored(self):
        relative = os.path.relpath(self.origin, start=os.getcwd())
        ws = workspace.clone(relative, "main", self.root)
        # git resolved the relative path to absolute in remote.origin.url; the config-side hash
        # must be computed from the same relative string and still match.
        self.assertTrue(workspace.is_worktree(ws, self.root, workspace.repo_url_sha256(relative)))

    def test_a_trailing_slash_or_dot_git_on_the_configured_repo_url_is_cosmetic(self):
        ws = workspace.clone(str(self.origin), "main", self.root)
        for variant in (str(self.origin) + "/", str(self.origin) + ".git", str(self.origin) + ".git/"):
            with self.subTest(variant=variant):
                self.assertTrue(workspace.is_worktree(ws, self.root, workspace.repo_url_sha256(variant)))

    def test_scp_like_host_colon_path_with_no_user_is_not_treated_as_a_local_path(self):
        """t_b0adfca3 goal 2: `_SCP_LIKE` only recognised `user@host:path`; a bare `host:repo`
        (no `user@` - a legal scp-like remote, e.g. a host with a default-user ssh config) fell
        through to the local-path branch and got resolved against the process cwd. Two independent
        oracles: (1) the hash matches hashing the raw scp string directly, the behaviour
        `_normalize_origin`'s docstring promises for scp-like input; (2) it does NOT match hashing
        what the (buggy) local-path resolution of the same string would have produced, which is
        what the bug actually did before this fix."""
        scp = "build-host.internal:repos/some-repo"
        self.assertEqual(workspace.repo_url_sha256(scp), hashlib.sha256(scp.encode()).hexdigest())
        resolved_as_local_path = str(Path(scp).expanduser().resolve())
        self.assertNotEqual(resolved_as_local_path, scp)   # sanity: cwd resolution really would differ
        self.assertNotEqual(workspace.repo_url_sha256(scp),
                            hashlib.sha256(resolved_as_local_path.encode()).hexdigest())

    def test_user_at_host_colon_path_scp_form_still_works(self):
        """The pre-existing `user@host:path` case must keep matching itself (no regression from
        making the user part optional)."""
        scp = "git@build-host.internal:repos/some-repo"
        self.assertEqual(workspace.repo_url_sha256(scp), hashlib.sha256(scp.encode()).hexdigest())


class WhyNotWorktree(unittest.TestCase):
    """Round-3 fix, R4: the refusal names which specific check failed, never the repo_url or the
    origin value itself (R3: those may carry an embedded credential, and this reason string is
    what worker.py folds into the typed event's `error`, which reaches Korin's evidence)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.origin = self.dir / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "README.md").write_text("x\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.root = self.dir / "root"
        self.repo_url_sha256 = workspace.repo_url_sha256(str(self.origin))

    def test_a_genuine_worktree_gives_no_reason(self):
        ws = workspace.clone(str(self.origin), "main", self.root)
        self.assertIsNone(workspace.why_not_worktree(ws, self.root, self.repo_url_sha256))

    def test_a_symlink_names_the_symlink_check(self):
        ws = workspace.clone(str(self.origin), "main", self.root)
        link = self.root / "link"
        link.symlink_to(ws)
        self.assertIn("symlink", workspace.why_not_worktree(link, self.root, self.repo_url_sha256))

    def test_an_origin_mismatch_names_the_origin_check_without_the_url(self):
        other = self.dir / "other-origin"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        (other / "f.txt").write_text("y\n")
        git(other, "add", ".")
        git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        ws = workspace.clone(str(other), "main", self.root)
        reason = workspace.why_not_worktree(ws, self.root, self.repo_url_sha256)
        self.assertIn("repo_url", reason)
        self.assertIn("re-clone", reason)
        self.assertNotIn(str(self.origin), reason)
        self.assertNotIn(str(other), reason)


class SyncToOrigin(unittest.TestCase):
    """t_de1466d6, 2026-09-27: `clone()` fetches `origin/<branch>` but never fast-forwards a
    branch already checked out (`workspace.tip`'s own docstring names the gap); `sync_to_origin`
    closes it, called by `Tick._claim_and_launch` before `start_sha` is pinned. The behavioural
    oracles for `contracts/intent/preflight.jsonl`'s two `sync_to_origin` examples live here; its
    third example (the missing work_root/repo_url_sha256 case) is
    `IsWorktree.test_missing_root_or_repo_url_is_refused_not_a_crash`, above."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.origin = self.dir / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "README.md").write_text("x\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.root = self.dir / "root"

    def test_a_clone_strictly_behind_origin_fast_forwards_clean(self):
        """preflight.jsonl: epic-clone-strictly-behind-origin-fast-forwards. The exact incident
        shape (t_de1466d6): the host branch gets a commit the clone already fetched into its
        remote-tracking ref but never checked out - `sync_to_origin` brings it up to that
        already-fetched tip, ff-only, and reports nothing wrong."""
        ws = workspace.clone(str(self.origin), "main", self.root)
        before = workspace.head(ws)
        (self.origin / "README.md").write_text("y\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "second")
        git(ws, "fetch", "origin", "main")   # what `clone()`'s reuse path already does
        self.assertEqual(workspace.head(ws), before)   # not yet fast-forwarded - the gap
        origin_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.origin),
                                    capture_output=True, text=True, check=True).stdout.strip()
        self.assertIsNone(workspace.sync_to_origin(ws))
        self.assertEqual(workspace.head(ws), origin_sha)

    def test_a_clone_diverged_from_origin_is_refused_with_both_shas(self):
        """preflight.jsonl: epic-clone-diverged-from-origin-refused-with-both-shas. The clone has
        a commit of its own that `origin/<branch>` does not (a run committed locally, or the host
        branch was rewritten) - fast-forwarding would discard it, so `sync_to_origin` refuses and
        names both shas instead, leaving the clone exactly as it was."""
        ws = workspace.clone(str(self.origin), "main", self.root)
        before = workspace.head(ws)
        (self.origin / "README.md").write_text("y\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "origin-only")
        git(ws, "fetch", "origin", "main")
        origin_sha = subprocess.run(["git", "rev-parse", "origin/main"], cwd=str(ws),
                                    capture_output=True, text=True, check=True).stdout.strip()
        (ws / "local.txt").write_text("z\n")
        git(ws, "add", ".")
        git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local-only")
        local_sha = workspace.head(ws)
        self.assertNotEqual(local_sha, before)
        result = workspace.sync_to_origin(ws)
        self.assertIsNotNone(result)
        error_class, reason = result
        self.assertEqual(error_class, "worktree_mismatch")
        self.assertIn(local_sha, reason)
        self.assertIn(origin_sha, reason)
        self.assertEqual(workspace.head(ws), local_sha)   # untouched - never forced through

    def test_a_dirty_clone_behind_origin_is_refused_with_the_path_list_and_never_ffd(self):
        """The live epic clone was found 15 commits behind origin AND dirty (10 modified files)
        at the same time, 2026-09-27. A fast-forward over uncommitted changes could discard or
        silently merge with them, so `sync_to_origin` must refuse before ever running `merge
        --ff-only` - never `checkout`/`reset`/`clean` to clear the way. The refusal is
        `worktree_dirty`, distinct from a real divergence, and names every dirty path."""
        ws = workspace.clone(str(self.origin), "main", self.root)
        before = workspace.head(ws)
        (self.origin / "README.md").write_text("y\n")
        git(self.origin, "add", ".")
        git(self.origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "second")
        git(ws, "fetch", "origin", "main")
        (ws / "README.md").write_text("uncommitted local edit\n")
        (ws / "untracked.txt").write_text("also dirty\n")
        result = workspace.sync_to_origin(ws)
        self.assertIsNotNone(result)
        error_class, reason = result
        self.assertEqual(error_class, "worktree_dirty")
        self.assertIn("README.md", reason)
        self.assertIn("untracked.txt", reason)
        self.assertEqual(workspace.head(ws), before)   # never fast-forwarded
        self.assertEqual((ws / "README.md").read_text(), "uncommitted local edit\n")   # untouched

    def test_a_clone_strictly_ahead_of_origin_is_left_alone(self):
        """Direct oracle for the `base == remote` guard: a clone with its own commit origin
        does not yet have (normal epic progress - a prior card's work) is not a mismatch and
        needs no sync. Only `test_tick.py`'s happy-path round exercised this before, indirectly;
        this is the guard's own test."""
        ws = workspace.clone(str(self.origin), "main", self.root)
        (ws / "local.txt").write_text("z\n")
        git(ws, "add", ".")
        git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ahead")
        ahead_sha = workspace.head(ws)
        self.assertIsNone(workspace.sync_to_origin(ws))
        self.assertEqual(workspace.head(ws), ahead_sha)   # unchanged


class CloneFailureRedaction(unittest.TestCase):
    """t_b0adfca3 goal 1: `clone()`'s failure is the only string that ever leaves this module for
    a failing clone - `tick.py`'s `_ruling_of`/launch call sites fold it straight into
    `error=f"{type(exc).__name__}: {exc}"[:2000]`, which `lease.end_run` writes to the task_run
    row and `event(...)` writes to the typed event, both of which reach tick.log and Korin's
    evidence. A `repo_url` with an embedded token (a private https remote) must never
    round-trip through that string. Uses `file://` (no
    network) so the failure is a genuine, reproducible non-existent-repository error - the string
    handling this exercises is transport-agnostic, so it covers the https/token-in-URL shape too."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)
        self.root = self.dir / "root"
        # Obviously-fake token, per the brief's "no secrets" constraint.
        self.token = "FAKE-TOKEN-do-not-use-9f8e7d6c5b4a"

    def test_a_failing_clone_with_a_token_in_the_url_never_leaks_it(self):
        bad = f"file://{self.dir}/nowhere-{self.token}.git"
        with self.assertRaises(RuntimeError) as cm:
            workspace.clone(bad, "main", self.root)
        message = str(cm.exception)
        # Oracle 1: the token substring itself is gone from the raised message.
        self.assertNotIn(self.token, message)
        # Oracle 2 (independent check, not just "no token"): the exact repo_url is gone too, and
        # the one non-secret correlation id job.json uses in its place - repo_url_sha256 -
        # is present, so the failure stays traceable without ever carrying the secret.
        self.assertNotIn(bad, message)
        self.assertIn(workspace.repo_url_sha256(bad), message)

    def test_a_successful_clone_is_unaffected(self):
        """The redaction wraps only the failure path; an ordinary clone still returns the
        workspace and is still a real, verifiable worktree."""
        origin = self.dir / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        (origin / "README.md").write_text("x\n")
        git(origin, "add", ".")
        git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        ws = workspace.clone(str(origin), "main", self.root)
        self.assertTrue(workspace.is_worktree(ws, self.root, workspace.repo_url_sha256(str(origin))))

    def test_a_failing_fetch_on_the_reuse_path_never_leaks_the_token_either(self):
        """Fix round 1, I2: the reuse path's `git fetch` resolves `origin` to the git dir's own
        stored remote URL and can echo it (or a credential in it) in stderr on failure, exactly
        like the clone step - so it needs the same redaction. Build the origin at a path holding
        the fake token, `file://` clone it once, remove it, then fail the reuse-path fetch."""
        origin = self.dir / f"origin-{self.token}"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        (origin / "README.md").write_text("x\n")
        git(origin, "add", ".")
        git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        bad = f"file://{origin}"
        workspace.clone(bad, "main", self.root)
        shutil.rmtree(origin)   # origin now unreachable: a reuse-path fetch will fail
        with self.assertRaises(RuntimeError) as cm:
            workspace.clone(bad, "main", self.root)
        message = str(cm.exception)
        self.assertNotIn(self.token, message)
        self.assertNotIn(bad, message)
        self.assertIn(workspace.repo_url_sha256(bad), message)

    def test_a_clone_timeout_never_leaks_the_url(self):
        """Fix round 1, I1: `_git`'s `subprocess.run(..., timeout=...)` raises
        `subprocess.TimeoutExpired` on a timeout, whose `str()` carries the full argv including
        `repo_url` - a plain `except RuntimeError` misses it entirely. `GIT_SLOW_TIMEOUT` (the
        constant, not the subject) is patched absurdly small so any real clone times out."""
        origin = self.dir / f"origin-{self.token}"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        (origin / "README.md").write_text("x\n")
        git(origin, "add", ".")
        git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        bad = f"file://{origin}"
        with mock.patch.object(workspace, "GIT_SLOW_TIMEOUT", 0.0001):
            with self.assertRaises(RuntimeError) as cm:
                workspace.clone(bad, "main", self.root)
        message = str(cm.exception)
        self.assertNotIn(self.token, message)
        self.assertNotIn(bad, message)
        self.assertIn(workspace.repo_url_sha256(bad), message)

    def test_a_validate_repo_url_error_never_leaks_the_token(self):
        """Same-round ruling: `_validate_repo_url`'s ValueErrors (a leading '-', or an
        `ext::`/`fd::` transport) still interpolated the raw url. Fix round 2, R2-I1: these are
        two separate `raise`s (workspace.py:109 and :111) - the leading-'-' case alone left the
        `ext::`/`fd::` one uncovered, and the round-2 reviewer's hand mutation putting `{url!r}`
        back into just that second raise still passed every test. `fd::` shares the same raise as
        `ext::`, so one `ext::` case stands in for both."""
        for bad in (f"-{self.token}", f"ext::sh -c {self.token}"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as cm:
                    workspace.clone(bad, "main", self.root)
                message = str(cm.exception)
                self.assertNotIn(self.token, message)
                self.assertIn(workspace.repo_url_sha256(bad), message)


class PreflightIntentExamples(unittest.TestCase):
    """Row 470 I8 (honest test), followed here as `test_unblocked.IntentExamples` does: every row
    454 example in `contracts/intent/preflight.jsonl` is run through the real check it names
    (`why_not_worktree` or `sync_to_origin`) against a git fixture built to match its `given`, not
    merely linted for shape - and must produce the `expect` it declares."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(tmpclean.sweep, self.dir)

    def _fresh_repo(self, name):
        origin = self.dir / f"origin-{name}"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        (origin / "README.md").write_text("x\n")
        git(origin, "add", ".")
        git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        return origin, self.dir / f"root-{name}"

    def _head(self, cwd):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd),
                              capture_output=True, text=True, check=True).stdout.strip()

    def _behind_clean(self):
        origin, root = self._fresh_repo("behind")
        ws = workspace.clone(str(origin), "main", root)
        (origin / "README.md").write_text("y\n")
        git(origin, "add", ".")
        git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "second")
        git(ws, "fetch", "origin", "main")
        return ws, self._head(origin)

    def _wal_db(self, name, *, wal_has_pending_frames, shm_present, dir_writable):
        """A real WAL-mode board db matching the `given.document` shape one of the `wal_health`
        (row 466) examples names, built by `wal_oracle`'s separate writer process (review I2:
        a same-process writer/reader pair silently shares sqlite's own `-shm` mapping and would
        hide the very failure a real reader hits)."""
        path = wal_oracle.make(self.dir / name, frames=wal_has_pending_frames)
        if not shm_present:
            (Path(str(path) + "-shm")).unlink(missing_ok=True)
        if not dir_writable:
            path.parent.chmod(0o555)
            self.addCleanup(path.parent.chmod, 0o755)   # so tempdir cleanup can still remove it
        return path

    def _diverged(self):
        origin, root = self._fresh_repo("diverged")
        ws = workspace.clone(str(origin), "main", root)
        (origin / "README.md").write_text("y\n")
        git(origin, "add", ".")
        git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "second")
        git(ws, "fetch", "origin", "main")
        origin_tip = self._head(origin)
        (ws / "local.txt").write_text("z\n")
        git(ws, "add", ".")
        git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local-only")
        return ws, origin_tip

    def test_every_example_matches_its_oracle(self):
        """R2-3: `sync_to_origin`'s outcome is checked against the real repository state (HEAD,
        both shas), never against the fixture's own `expect` JSON echoed back - a no-op
        `sync_to_origin` that always returns None would still pass a `assertTrue(expect[...])`
        check, which is why the behind example now asserts `head(ws) == origin_tip` directly."""
        lines = (paths.INTENT / "preflight.jsonl").read_text().splitlines()
        self.assertTrue(lines)
        for line in lines:
            row = json.loads(line)
            doc, expect = row["given"]["document"], row["expect"]
            if doc["check"] == "why_not_worktree":
                reason = workspace.why_not_worktree(self.dir / "never-cloned", None, None)
                self.assertEqual(reason is not None, expect["refused"], row["id"])
                self.assertEqual(reason, expect["reason"], row["id"])
            elif doc.get("local_has_commits_origin_lacks"):
                ws, origin_tip = self._diverged()
                local_sha = workspace.head(ws)
                result = workspace.sync_to_origin(ws)
                self.assertEqual(result is not None, expect["refused"], row["id"])
                self.assertEqual(result[0], "worktree_mismatch", row["id"])
                self.assertIn(local_sha, result[1], row["id"])
                self.assertIn(origin_tip, result[1], row["id"])
                self.assertEqual(workspace.head(ws), local_sha, row["id"])   # HEAD unchanged
            elif doc.get("local_is_ancestor_of_origin"):
                ws, origin_tip = self._behind_clean()
                result = workspace.sync_to_origin(ws)
                self.assertEqual(result is None, not expect["refused"], row["id"])
                self.assertEqual(workspace.head(ws), origin_tip, row["id"])   # actually fast-forwarded
            elif doc["check"] == "wal_health":
                # row 466 review C2: the oracle is the real open (`connect_ro_tolerant`, the
                # same one `preflight.py` uses), never a prediction of what sqlite would do.
                path = self._wal_db(
                    row["id"], wal_has_pending_frames=doc["wal_has_pending_frames"],
                    shm_present=doc["shm_present"], dir_writable=doc["dir_writable"])
                try:
                    capsule_db.connect_ro_tolerant(path).close()
                    ok = True
                except sqlite3.OperationalError:
                    ok = False
                self.assertEqual(ok, expect["ok"], row["id"])
            elif doc["check"] == "slack_owner_id":
                # the real row against a temp home (never the real config); never the value
                with tempfile.TemporaryDirectory() as home:
                    (Path(home) / "config.json").write_text(json.dumps(doc["config"]))
                    level, name, detail = preflight._slack_owner_row(Path(home))
                self.assertEqual(level, expect["level"], row["id"])
                self.assertEqual(name, "slack_owner_id", row["id"])
                self.assertNotIn("U0ABC1234", detail, row["id"])
                self.assertEqual("Slack button answers are refused" in detail,
                                 expect["says_button_answers_refused"], row["id"])
            else:
                self.fail(f"no oracle wired for {row['id']}")

    def test_wal_health_examples_are_shipped(self):
        """Companion to the loop above, independent of it: the loop's own `wal_health`
        coverage disappears along with `preflight.jsonl`'s rows if the mutation gate ever
        reverts `db.py` and the jsonl together (the loop then has nothing new to iterate -
        a silent pass, not a catch). This reads the count straight off the file with no
        `capsule.db` call at all, so it fails on a reverted jsonl for a plain, unrelated
        reason (a count mismatch, never an `AttributeError` on a reverted symbol)."""
        lines = (paths.INTENT / "preflight.jsonl").read_text().splitlines()
        rows = [json.loads(l) for l in lines if l.strip()]
        wal_rows = [r for r in rows if r["given"]["document"].get("check") == "wal_health"]
        self.assertEqual(len(wal_rows), 3, "expected 3 wal_health intent examples (row 466)")

    def test_slack_owner_id_examples_are_shipped(self):
        """Same companion pattern as `wal_health` above: a plain count off the jsonl, so a
        revert of the rows together with `preflight.py` fails here instead of passing silently."""
        lines = (paths.INTENT / "preflight.jsonl").read_text().splitlines()
        rows = [json.loads(ln) for ln in lines if ln.strip()]
        slack_rows = [r for r in rows if r["given"]["document"].get("check") == "slack_owner_id"]
        self.assertEqual(len(slack_rows), 2, "expected 2 slack_owner_id intent examples")

    def test_wal_health_oracle_flags_the_reproduced_bad_state(self):
        """Companion to the two above: exercises `db.connect_ro_tolerant` directly against a
        hardcoded fixture matching the reproduced bad state (row 466), independent of
        whatever `preflight.jsonl` currently contains - the in-process reader capsule
        actually uses, replacing the old sqlite3-CLI oracle (review I2)."""
        path = wal_oracle.make(self.dir / "hardcoded-bad-state", frames=True)
        (path.parent / "kanban.db-shm").unlink()
        path.parent.chmod(0o555)
        self.addCleanup(path.parent.chmod, 0o755)
        with self.assertRaises(sqlite3.OperationalError):
            capsule_db.connect_ro_tolerant(path)
