"""paths.home() under AGENT_OPS_NO_SEND never reaches the real ~/.local/share/capsule.

Incident 2026-10-27: a single-module run (`discover -p <file>`, as tests/mutation_check.py does)
never imports test_0_tmpsweep, so tmpclean.install() did not run and a test that shelled out to
`python3 -m capsule pause` wrote a real pause:board into the live board DB. Mirrors
maintenance/events.py state_root() (row 609): the marker alone decides. Every case runs in a
child with HOME and TMPDIR pointed at a scratch dir, so a regression cannot touch the real home."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import db, intent, paths

REPO = Path(__file__).resolve().parents[2]
SNIPPET = "from capsule import paths; print(paths.home())"


class _Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(self._rm)
        self.fake_home = self.tmp / "home"
        self.scratch = self.tmp / "tmp"
        self.fake_home.mkdir()
        self.scratch.mkdir()
        self.trap = self.fake_home / ".local" / "share" / "capsule"
        self.no_send = self.scratch / "capsule-home-no-send"

    def _rm(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, no_send=True, capsule_home=None):
        drop = ("AGENT_OPS_NO_SEND", "CAPSULE_HOME")
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env.update(HOME=str(self.fake_home), TMPDIR=str(self.scratch))
        if no_send:
            env["AGENT_OPS_NO_SEND"] = "1"
        if capsule_home is not None:
            env["CAPSULE_HOME"] = str(capsule_home)
        return env

    def _run(self, env, *argv):
        return subprocess.run([sys.executable, *argv], cwd=REPO, env=env, capture_output=True,
                              text=True, timeout=120)

    def _home(self, env):
        out = self._run(env, "-c", SNIPPET)
        self.assertEqual(out.returncode, 0, out.stderr)
        return Path(os.path.realpath(out.stdout.strip()))

    def _pause_value(self, home):
        conn = db.connect(paths.board_db("b1", home))
        try:
            return db.guard_get(conn, "pause:board")[0]
        finally:
            conn.close()


class HomeNoSendTest(_Scratch):
    def test_no_send_resolves_under_temp_dir_never_the_home_default(self):
        got = self._home(self._env())
        self.assertEqual(got, self.no_send)
        self.assertNotIn(self.trap, (got, *got.parents))

    def test_no_send_pause_child_writes_nothing_under_the_home_default(self):
        db.create_board(paths.board_db("b1", self.trap))
        db.create_board(paths.board_db("b1", self.no_send))
        out = self._run(self._env(), "-m", "capsule", "pause", "--reason", "x")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(self._pause_value(self.trap), "pause reached the HOME default board")
        self.assertTrue(self._pause_value(self.no_send), "pause did not land in the no-send home")

    def test_explicit_capsule_home_is_honoured_even_with_no_send(self):
        want = self.tmp / "explicit"
        self.assertEqual(self._home(self._env(capsule_home=want)), want)

    def test_neither_variable_keeps_the_home_default(self):
        self.assertEqual(self._home(self._env(no_send=False)), self.trap)

    def test_capsule_home_without_no_send_is_honoured(self):
        want = self.tmp / "explicit"
        self.assertEqual(self._home(self._env(no_send=False, capsule_home=want)), want)


class IntentExamples(_Scratch):
    """contracts/intent/capsule_home.jsonl, run on the real code (Z1); an unknown shape fails."""

    def _sub(self, text):
        for key, val in (("$HOME", self.fake_home), ("$TMPDIR", self.scratch),
                         ("$SCRATCH", self.tmp)):
            text = text.replace(key, str(val))
        return text

    def test_every_example_holds_on_the_real_code(self):
        examples = intent.load("capsule_home")
        self.assertEqual(len(examples), 3)                # an absent file would pass vacuously
        for ex in examples:
            with self.subTest(ex["id"]):
                doc, exp = ex["given"]["document"], ex["expect"]
                self.assertEqual(doc["check"], "capsule_home")
                self.assertLessEqual(set(doc), {"check", "env"})
                self.assertLessEqual(set(exp), {"home", "pause_reaches_home_default"})
                self.assertIn("home", exp)
                env = self._env(no_send=False)
                env.update({k: self._sub(v) for k, v in doc["env"].items()})
                want = Path(os.path.realpath(self._sub(exp["home"])))
                self.assertEqual(self._home(env), want)
                if "pause_reaches_home_default" in exp:
                    self.assertIs(exp["pause_reaches_home_default"], False)
                    db.create_board(paths.board_db("b1", self.trap))
                    self._run(env, "-m", "capsule", "pause", "--reason", "x")
                    self.assertFalse(self._pause_value(self.trap))


if __name__ == "__main__":
    unittest.main()
