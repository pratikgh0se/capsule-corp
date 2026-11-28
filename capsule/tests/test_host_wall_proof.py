"""The host wall's start-up proof under the real seatbelt: a unix socket in a walled folder is a
refusal, not an "other" failure; two starts of different roles on one spool never run each other's
profile; a rogue allow deep inside a walled folder is found (S45, S46, S47)."""
import concurrent.futures
import contextlib
import io
import os
import socket
import unittest
from unittest import mock

from capsule.runners import host_wall
from capsule.tests.test_host_wall import DARWIN, NO_WRAP, Board


def bind_socket(folder, name="s.sock"):
    """A unix socket file `folder/name` (a sun_path holds ~100 characters: bound from inside)."""
    here = os.getcwd()
    os.chdir(folder)
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(name)
    finally:
        os.chdir(here)
    return folder / name


def start(args):
    """One start in its own process (module level: a process pool pickles it)."""
    role, wall = args
    try:
        return host_wall.apply(["claude", role], wall)[2]
    except host_wall.WallUnavailable as exc:
        return f"FAILED {exc}"


class WithFakeExe(Board):
    def fake_exe(self, text):
        exe = self.root / "sandbox-exec"
        exe.write_text(text)
        exe.chmod(0o755)
        return mock.patch.object(host_wall, "SANDBOX_EXEC", str(exe))


class Sockets(WithFakeExe):
    def places(self):
        (self.cfg / "jobs" / "sess").mkdir()
        return [self.repo, self.spools, self.other, self.cfg / "projects",
                self.cfg / "jobs" / "sess", self.board / "blind-work" / "r9",
                self.board / "blind-work"]

    @DARWIN
    def test_a_unix_socket_anywhere_in_a_walled_folder_does_not_fail_the_proof(self):
        for place in self.places():
            sock = bind_socket(place)
            wall = self.plan()
            self.assertIn(str(sock), host_wall._must_refuse(wall, "x")[1], place)   # it is tried
            host_wall.apply(["claude"], wall)                   # refused by stat: the wall holds
            with self.fake_exe(NO_WRAP), self.assertRaisesRegex(host_wall.WallUnavailable,
                                                                "did not refuse a walled path"):
                host_wall.apply(["claude"], wall)               # a socket let through still fails
            sock.unlink()


class OneSpool(WithFakeExe):
    @DARWIN
    def test_implementer_and_korin_starts_at_once_on_one_spool_each_prove_their_own_profile(self):
        walls = {role: self.plan(role, inputs={"evidence": [str(self.other)]} if role == "korin"
                                 else {}) for role in ("implementer", "korin")}
        jobs = [(role, walls[role]) for role in ("implementer", "korin") * 3]
        for _ in range(4):
            with concurrent.futures.ProcessPoolExecutor(6) as pool:
                got = list(pool.map(start, jobs))
            self.assertEqual([g for g in got if g.startswith("FAILED")], [])
            self.assertEqual(len(set(got)), 6)                  # a profile per start
            self.assertEqual(sorted(map(str, self.spool.glob("hostwall.sb*"))), sorted(set(got)))
            for old in self.spool.glob("hostwall.sb*"):
                old.unlink()
        self.assertEqual(list(self.spool.glob("hostwall.probe*")), [])

    @DARWIN
    def test_a_dropped_start_leaves_no_profile_file_behind(self):
        with self.fake_exe(NO_WRAP), self.assertRaises(host_wall.WallUnavailable):
            host_wall.apply(["claude"], self.plan())
        self.assertEqual(list(self.spool.glob("hostwall.sb*")), [])


class Deep(Board):
    def rogue(self, path):
        real = host_wall.render
        return mock.patch.object(host_wall, "render", lambda w: real(w) + (
            f'(allow file-read* (subpath "{path}"))\n'))

    @DARWIN
    def test_a_rogue_allow_deep_inside_a_walled_folder_is_caught(self):
        for top, names in ((self.repo, ("cards", "t_x", "deeper", "d4", "d5", "d6")),
                           (self.board / "blind-work" / "r9", ("run_tester", "sub", "subsub"))):
            for depth in range(3, len(names) + 1):
                deep = top.joinpath(*names[:depth])
                deep.mkdir(parents=True, exist_ok=True)
                (deep / "t.py").write_text("SECRET")
                wall = self.plan()
                host_wall.apply(["claude"], wall)               # the real profile holds
                with self.rogue(deep), self.assertRaisesRegex(
                        host_wall.WallUnavailable, "did not refuse a walled path",
                        msg=(top, depth)):
                    host_wall.apply(["claude"], wall)

    def start_logging(self, *allowed):
        """`apply` with a rogue allow for each of `allowed`: (what it raised, its stderr)."""
        wall, err = self.plan(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.ExitStack() as stack:
            for path in allowed:
                stack.enter_context(self.rogue(path))
            try:
                host_wall.apply(["claude"], wall)
            except host_wall.WallUnavailable as exc:
                return exc, err.getvalue()
        return None, err.getvalue()

    @DARWIN
    def test_a_tree_over_the_walks_limits_starts_and_logs_one_line_for_what_it_left_out(self):
        for n in range(300):
            (self.repo / "cards" / f"t_{n:04}" / "deeper").mkdir(parents=True)
            (self.repo / "cards" / f"t_{n:04}" / "deeper" / "t.py").write_text("SECRET")
        far = self.repo.joinpath(*"abcdefg")                   # seven levels down
        far.mkdir(parents=True)
        (far / "t.py").write_text("SECRET")
        for allowed in (self.repo / "cards" / "t_0299" / "deeper",
                        self.repo / "cards" / "t_0060" / "deeper", far):
            raised, log = self.start_logging(allowed)
            self.assertIsNone(raised, allowed)                 # a cut is no failure: it starts
            lines = [ln for ln in log.splitlines() if ln.startswith("host wall: not probed")]
            self.assertEqual(len(lines), 1, log)
            self.assertIn(f"under {self.repo}:", lines[0])
            self.assertRegex(lines[0], r"2\d\d past 50 in a folder")
            self.assertRegex(lines[0], r"\d+ past 200 paths")
            self.assertRegex(lines[0], r"1 below level 6")
        raised, log = self.start_logging(self.repo / "cards" / "t_0000" / "deeper")
        self.assertIsNotNone(raised)                           # inside the walk: still caught

    @DARWIN
    def test_a_tree_inside_the_limits_logs_nothing(self):
        (self.repo / "cards" / "t_1" / "deeper").mkdir(parents=True)
        raised, log = self.start_logging()
        self.assertEqual((raised, log), (None, ""))

    def test_the_walk_counts_what_each_limit_left_out(self):
        for n in range(60):  # plus the repo's own t.py: 61 entries
            (self.repo / f"w{n:02}").mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            host_wall._walk(str(self.repo))
        self.assertIn("11 past 50 in a folder, 0 past 200 paths, 0 below level 6", err.getvalue())
        deep = self.board / "blind-work" / "r9"
        deep.joinpath(*"abcdefgh").mkdir(parents=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            host_wall._walk(str(deep))
        self.assertIn("0 past 50 in a folder, 0 past 200 paths, 1 below level 6", err.getvalue())

    def test_the_walk_is_breadth_first_bounded_and_skips_symlinks(self):
        for n in range(60):
            (self.repo / f"d{n:02}" / "x" / "y").mkdir(parents=True)
        (self.repo / "link").symlink_to(self.other)
        got = host_wall._walk(str(self.repo))
        self.assertEqual((got[0], len(got) <= host_wall.MAX_WALK), (str(self.repo), True))
        self.assertNotIn(str(self.repo / "link"), got)
        self.assertNotIn(str(self.repo / "link" / "report.json"), got)
        depths = [p[len(str(self.repo)):].count("/") for p in got]
        self.assertEqual(depths, sorted(depths))                # shallow first
        self.assertIn(str(self.repo / "d00" / "x"), got)        # and below the first level
        deep = self.repo.joinpath(*"abcdefghij")
        deep.mkdir(parents=True)
        self.assertNotIn(str(deep), host_wall._walk(str(self.repo)))    # past the depth cap
        self.assertIn(str(self.repo.joinpath(*"abcdef")), host_wall._walk(str(self.repo)))


if __name__ == "__main__":
    unittest.main()
