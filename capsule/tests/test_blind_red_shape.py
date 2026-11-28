# mutation-check: targets-predate-range 4d253485 -- a test-only tightening (wave 10): rmtree is
#   mocked so the link guard, which already held on base, is what the test proves ::
#   Sweep.test_a_link_is_never_followed
"""The red check's containers look like a normal blind run's to the test inside them: a test that
reads /proc/self/mountinfo, its `/workspace/.git` pointer, `/.gitdirs`, its git branch or its file
times must not learn it is in the red check by accident or an easy trick. So the host paths are
made to look like a normal run's (`work/<name>/<slug>`, `<spool>/<run id>/blind`, `<spool>/<run
id>/blind-out`), the clone is on branch `main` with a `.worker-exec` folder as a worker leaves
it, and a killed check's leftovers are swept. The checkout has no real history (`checkout.build`,
the same for a blind run): HEAD, the last commit, the reflog and file times tell nothing. Temp repos
only; no docker.

Z2 oracles: what the strong runner is asked to start with and what git and the file system say of
the checkout at that moment (the pointer file, `git branch --show-current`, the listing), and the
work folder's listing after the pass."""
import os
import re
import subprocess
import uuid
from pathlib import Path
from unittest import mock

from capsule import paths, workspace
from capsule.blind import checkout, red
from capsule.blind import run as blind_run
from capsule.tests.test_blind_red_tick import RedBox, RedCase
from capsule.tests.test_blind_run import BOARD, Container, RunCase

HEX32, HEX8 = re.compile(r"[0-9a-f]{32}"), re.compile(r"[0-9a-f]{8}")
MARKER = "red-check"          # the on-disk name of the red run's marker file, pinned here


NEUTRAL = {"log": ["Capsule|capsule@localhost|946684800 +0000|Initial commit"],
           "refs": ["refs/heads/main"], "reflog": 0, "times": {946684800.0}}


def history(ws):
    """What a test can learn of the checkout's past with git and a file listing: every commit's
    identity, date and subject, every ref, the reflog's length and every file's and folder's
    mtime (the `.git` pointer file and `.worker-exec` included)."""
    def git(*args):
        return subprocess.run(["git", "-C", str(ws), *args], text=True,
                              capture_output=True).stdout.splitlines()
    return {"log": git("log", "--all", "--date=raw", "--format=%an|%ae|%ad|%s"),
            "refs": git("for-each-ref", "--format=%(refname)"),
            "reflog": len(git("reflog", "show", "HEAD")),
            "times": {p.lstat().st_mtime for p in [ws, *ws.rglob("*")]
                      if p == ws or p.relative_to(ws).parts[0] != ".git"}}


class Seen(RedBox):
    """Records what the checkout looks like when a container is started on it."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def start(self, run_id, ws, **kw):
        ws = Path(ws)
        branch = subprocess.run(["git", "-C", str(ws), "branch", "--show-current"], text=True,
                                capture_output=True).stdout.strip()
        snap = Path(kw["mounts"][0].split(":")[0])
        times = {p.stat().st_mtime for p in (snap / "cards").rglob("*")}
        head = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], text=True,
                              capture_output=True).stdout.strip()
        scratch = ws / ".worker-exec"
        ignored = subprocess.run(["git", "-C", str(ws), "status", "--ignored", "--porcelain"],
                                 text=True, capture_output=True).stdout
        self.seen.append({"pointer": (ws / ".git").read_text(), "branch": branch, "head": head,
                          "history": history(ws),
                          "worker_exec": scratch.is_dir(), "times": times, "ignored": ignored,
                          "mode": scratch.stat().st_mode & 0o7777,
                          "gitignore": (scratch / ".gitignore").read_text(),
                          "marker": (snap.parent / MARKER).is_file()})
        return super().start(run_id, ws, **kw)


class RedShape(RedCase):
    def started(self, box=None):
        self.make_round()
        self.step(box)
        return [(r, Path(w), Path(kw["git_dir"]), *(Path(m.split(":")[0]) for m in kw["mounts"]))
                for r, w, kw in self.box.started]

    def work(self):
        return paths.board_dir(BOARD, self.tick.home) / "work"

    def test_the_checkout_is_a_slug_folder_in_a_hex_folder_of_the_boards_work_folder(self):
        rows = self.started()
        self.assertEqual(len(rows), red.RUNS)
        for _, ws, gd, _, _ in rows:
            self.assertEqual((ws.name, ws.parent.parent), ("main", self.work()))
            self.assertTrue(HEX8.fullmatch(ws.parent.name))
            self.assertEqual(gd, ws.parent / ".gitdirs" / "main")
        self.assertEqual(len({r[1] for r in rows}), 1)          # one checkout of the base

    def test_the_pointer_the_container_reads_is_a_normal_clones(self):
        box = Seen()
        self.started(box)
        want = "gitdir: ../.gitdirs/main\n"
        self.assertEqual([s["pointer"] for s in box.seen], [want] * red.RUNS)

    def test_the_clone_is_on_main_and_has_a_worker_exec_folder(self):
        box = Seen()
        self.started(box)
        self.assertEqual([(s["branch"], s["worker_exec"]) for s in box.seen],
                         [("main", True)] * red.RUNS)

    def test_the_scratch_folder_is_as_a_worker_container_leaves_it(self):
        """`docker/worker/worker-entrypoint.sh`: mode 1777 and a `.gitignore` of `*`."""
        box = Seen()
        self.started(box)
        self.assertEqual([(s["mode"], s["gitignore"], s["ignored"]) for s in box.seen],
                         [(0o1777, "*\n", "!! .worker-exec/\n")] * red.RUNS)

    def test_a_run_has_the_marker_the_sweep_looks_for_in_its_spool_and_not_in_a_mount(self):
        box = Seen()
        rows = self.started(box)
        self.assertEqual([s["marker"] for s in box.seen], [True] * red.RUNS)
        self.assertFalse(any(p.name == MARKER for r in rows for p in r[3:]))

    def test_a_box_named_like_a_live_clone_is_a_retry_and_the_clone_is_kept(self):
        taken = self.work() / "0a1b2c3d"
        taken.mkdir(parents=True)
        (taken / "keep.txt").write_text("live")
        with mock.patch.object(red.uuid, "uuid4", return_value=uuid.UUID(int=0x0a1b2c3d << 96)):
            self.make_round()
            self.step()
        self.assertEqual((taken / "keep.txt").read_text(), "live")
        self.assertEqual((len(self.events("blind_red_retry")), self.box.started), (1, []))

    def test_the_checkout_is_not_the_base_and_has_no_real_history_or_file_age(self):
        box = Seen()
        self.started(box)
        self.assertNotIn(self.base, {s["head"] for s in box.seen})
        self.assertEqual(len({s["head"] for s in box.seen}), 1)
        self.assertEqual([s["history"] for s in box.seen], [NEUTRAL] * red.RUNS)

    def test_the_snapshot_and_out_have_the_shape_of_a_normal_runs(self):
        spool = self.root / "spool"
        rows = self.started()
        for run_id, _, _, snap, out in rows:
            self.assertTrue(HEX32.fullmatch(run_id))
            self.assertEqual((snap, out), (spool / run_id / blind_run.SNAP_DIR,
                                           spool / run_id / blind_run.OUT_DIR))
        self.assertEqual(len({r[0] for r in rows}), red.RUNS)

    def test_no_host_path_names_the_check_or_a_run_number(self):
        for row in self.started():
            for path in row[1:]:
                text = str(path.relative_to(self.root))
                self.assertNotIn("blind-red", text)
                self.assertNotRegex(text, r"blind-out\d|/\d$")

    def test_every_file_of_the_cards_tests_in_the_snapshot_has_the_one_fixed_mtime(self):
        box = Seen()
        self.started(box)
        self.assertEqual([s["times"] for s in box.seen], [{blind_run.SNAP_MTIME}] * red.RUNS)

    def test_nothing_of_the_check_is_left_after_a_pass_or_a_raise(self):
        class Boom(RedBox):
            def exec(self, name, cmd, timeout):
                raise RuntimeError("docker is gone")

        self.started()
        self.assertEqual(list(self.work().glob("*")), [])
        self.assertEqual(list((self.root / "spool").glob("*")), [])
        self.make_round()
        self.step(Boom())
        self.assertEqual((list(self.work().glob("*")), list((self.root / "spool").glob("*"))),
                         ([], []))

    def test_an_epics_own_clone_in_the_work_folder_is_left_alone(self):
        (self.work() / ".gitdirs" / "main").mkdir(parents=True)
        (self.work() / "main").mkdir()
        (self.work() / "main" / "keep.txt").write_text("x")
        self.started()
        self.assertEqual((self.work() / "main" / "keep.txt").read_text(), "x")
        self.assertTrue((self.work() / ".gitdirs" / "main").is_dir())
        self.assertEqual(len(self.events("blind_landed")), 1)

    def test_a_git_failure_reaches_the_retry_reason_without_the_repo_url(self):
        self.cfg["repo_url"] = str(self.root / "secret-token-repo")
        self.make_round()
        self.step()
        (retry,) = self.events("blind_red_retry")
        self.assertNotIn("secret-token-repo", retry["reason"])
        self.assertIn("sha256:", retry["reason"])


class Sweep(RedCase):
    def stale(self, name, kids=(".gitdirs", "main"), old=True):
        box = paths.board_dir(BOARD, self.tick.home) / "work" / name
        for kid in kids:
            (box / kid).mkdir(parents=True)
        if old:
            os.utime(box, (1, 1))
        return box

    def swept(self, box):
        self.step()
        return not os.path.lexists(box)

    def test_an_old_hex_folder_with_gitdirs_is_removed_on_the_next_pass(self):
        self.assertTrue(self.swept(self.stale("0a1b2c3d")))

    def test_a_young_one_is_kept(self):
        self.assertFalse(self.swept(self.stale("0a1b2c3d", old=False)))

    def test_nothing_else_in_work_is_touched(self):
        keep = [self.stale("epic-x", kids=(".gitdirs",)), self.stale("0a1b2c3", kids=(".gitdirs",)),
                self.stale("0a1b2c3de", kids=(".gitdirs",)), self.stale("0A1B2C3D"),
                self.stale("1a1b2c3d", kids=("main",)), self.stale("2a1b2c3d", kids=(".gitdirs",
                                                                                    "a", "b"))]
        gitdirs = self.stale(".gitdirs", kids=("main",))
        self.step()
        self.assertEqual([p.exists() for p in [*keep, gitdirs]], [True] * (len(keep) + 1))

    def test_a_link_is_never_followed(self):
        target = self.root / "elsewhere"
        (target / ".gitdirs").mkdir(parents=True)
        os.utime(target, (1, 1))
        work = paths.board_dir(BOARD, self.tick.home) / "work"
        work.mkdir(parents=True)
        os.symlink(target, work / "0a1b2c3d")
        with mock.patch.object(red.shutil, "rmtree") as removed:   # rmtree itself refuses a link
            self.step()
        self.assertTrue((target / ".gitdirs").is_dir())
        removed.assert_not_called()

    def test_a_live_clone_with_a_hex_slug_and_a_tracked_gitdirs_folder_is_kept(self):
        """A branch slug of 8 hex digits whose clone holds a tracked `.gitdirs/`: a live clone
        always has its git dir at `work/.gitdirs/<slug>`, a red box never does."""
        clone = self.stale("0a1b2c3d", kids=(".gitdirs",))
        (clone.parent / ".gitdirs" / "0a1b2c3d").mkdir(parents=True)
        self.assertFalse(self.swept(clone))

    def spool_run(self, name="a" * 32, marker=True, old=True):
        run = self.root / "spool" / name
        run.mkdir(parents=True)
        (run / "blind-out").mkdir()
        if marker:
            (run / MARKER).touch()
            if old:
                os.utime(run / MARKER, (1, 1))
        return run

    def test_an_old_red_spool_is_removed_on_the_next_pass(self):
        self.assertTrue(self.swept(self.spool_run()))

    def test_a_young_red_spool_is_kept(self):
        self.assertFalse(self.swept(self.spool_run(old=False)))

    def test_a_spool_with_no_marker_is_kept_whatever_its_age(self):
        """A blind run's own spool (a validator's run id has no `task_runs` row yet)."""
        run = self.spool_run(marker=False)
        os.utime(run, (1, 1))
        self.assertFalse(self.swept(run))

    def test_a_marker_that_is_a_link_or_a_spool_that_is_a_link_is_not_followed(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (target / MARKER).touch()
        os.utime(target / MARKER, (1, 1))
        (self.root / "spool").mkdir()
        os.symlink(target, self.root / "spool" / ("b" * 32))
        run = self.root / "spool" / ("c" * 32)
        run.mkdir()
        os.symlink(target / MARKER, run / MARKER)
        self.step()
        self.assertEqual((target / MARKER).exists(), True)
        self.assertTrue(run.exists())


class NormalRunSnapshot(RunCase):
    def test_a_normal_runs_snapshot_has_the_same_fixed_mtime(self):
        times = []

        class Peek(Container):
            def start(self, run_id, ws, **kw):
                snap = Path(kw["mounts"][0].split(":")[0])
                times.append({p.stat().st_mtime for p in (snap / "cards").rglob("*")})
                return super().start(run_id, ws, **kw)

        self.go(Peek(self.ws))
        self.assertEqual(times, [{blind_run.SNAP_MTIME}])

    def test_a_normal_run_sees_the_cards_uncommitted_work(self):
        """The card's work is committed only after the gate, so the run must see the worktree."""
        (self.ws / "hello.txt").write_text("edited\n")              # an uncommitted edit
        (self.ws / "extra.txt").write_text("new\n")                 # a new, untracked file
        (self.ws / ".worker-exec").mkdir()
        (self.ws / ".worker-exec" / "scratch").write_text("s")
        seen = []

        class Peek(Container):
            def start(self, run_id, ws, **kw):
                ws = Path(ws)
                seen.append((sorted(p.name for p in ws.iterdir()), (ws / "hello.txt").read_text(),
                             sorted(p.name for p in (ws / ".worker-exec").iterdir())))
                return super().start(run_id, ws, **kw)

        rows = self.go(Peek(self.ws))
        self.assertEqual(seen, [([".git", ".worker-exec", "extra.txt", "hello.txt"], "edited\n",
                                 [".gitignore"])])           # the card's scratch is not copied
        greets = next(r for r in rows if r["cmd"].endswith("test_greets"))
        self.assertEqual(greets["exit"], 1)                         # it read "edited", not "hi"
        self.assertIn("edited", greets["output"])

    def test_too_many_untracked_files_make_the_run_inconclusive_with_no_container(self):
        for n in range(3):
            (self.ws / f"u{n}.txt").write_text("u\n")
        box = Container(self.ws)
        with mock.patch.object(checkout, "MAX_UNTRACKED", 2):
            rows = self.go(box)
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertIn("3 untracked files that are not ignored (over 2)", rows[0]["output"])
        self.assertEqual(box.started, [])                          # nothing was run

    def test_a_card_worktree_that_changes_during_the_run_makes_it_inconclusive(self):
        orig = self.ws

        class Late(Container):
            def exec(self, name, cmd, timeout):
                (orig / "late.txt").write_text("written while the tests ran")
                return super().exec(name, cmd, timeout)

        rows = self.go(Late(self.ws))
        self.assertEqual([r["cmd"] for r in rows], ["blind:inconclusive"])
        self.assertIn("worktree changed while the blind run ran", rows[0]["output"])

    def test_a_normal_runs_checkout_is_the_same_neutral_one_as_the_red_checks(self):
        seen = []

        class Peek(Container):
            def start(self, run_id, ws, **kw):
                seen.append((Path(ws), history(Path(ws)), Path(ws).parent.name,
                             subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], text=True,
                                            capture_output=True).stdout.strip()))
                return super().start(run_id, ws, **kw)

        self.go(Peek(self.ws))
        ((ws, hist, box, head),) = seen
        self.assertEqual(hist, NEUTRAL)
        self.assertNotIn(head, (self.base, workspace.head(self.ws)))
        self.assertNotEqual(ws, self.ws)                    # not the card's own worktree
        self.assertTrue(HEX8.fullmatch(box), box)           # work/<8 hex>/<name>, as a red check's
        self.assertEqual(ws.parent.parent, paths.board_dir(BOARD, self.tick.home) / "work")
        self.assertFalse(ws.parent.exists())                # removed after the run
