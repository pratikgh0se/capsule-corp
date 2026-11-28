"""Row 509 size-gate refactor: `capsule.__main__.main` became argument parsing plus a
dispatch dict. Runs `python3 -m capsule <cmd> ...` end-to-end (subprocess, same pattern as
test_main_override.py, always from `paths.REPO` so a caller's own cwd can never change what
gets imported) for every real subcommand, and checks two things a dropped or miswired
DISPATCH entry breaks: (1) `INVOCATIONS` names every subcommand the parser actually has, read
from `--help`'s own usage line, so a new subcommand can never go uncovered here; (2) each
invocation's output carries a marker that only appears once its OWN handler has actually run
(never merely that argparse accepted the subcommand, or that some other handler ran instead) -
a raw `KeyError: '<cmd>'` from a dropped entry, or a working but WRONG handler from a miswired
one, both leave that command's own marker absent.

This test's own source never imports `capsule.__main__`/`capsule.tick` directly (only
`capsule.paths`, unchanged by this range) - see the earlier report for why: mutation_check's
revert-mutation column, comparing against `--base` where `DISPATCH`/`_build_parser` do not
exist at all, treats a direct import of either as a pure addition and "keeps" the whole file
at HEAD to get past collection, which then trivially passes without ever discriminating."""
import os
import platform
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import paths

# command -> (extra argv, (stream, marker)): each marker is text that appears ONLY once that
# command's own handler has genuinely run - never just because argparse accepted the
# subcommand (a dropped DISPATCH entry still gets that far) and never text a DIFFERENT
# handler would also produce (a miswired entry, e.g. "report": _cmd_check, must still be
# caught).
INVOCATIONS = {
    # Task 9 round-2 M2: a bare `--capsule-home` with no config.json refuses (exit 3) before
    # ever reaching `Tick.once()`'s own "config_error" counter - see _cmd_tick's single config
    # read (Opus review fix round 1, finding 1).
    "tick": (["--once"], ("stderr", "config unreadable")),
    "run-one": (["{home}/board.db", "{home}/spool", "x", "x"], ("stderr", "worker")),
    "report": (["x"], ("stderr", "capsule report:")),
    "check": ([], ("stdout", "FAIL config:")),
    "unblocked": (["--json"], ("stdout", '{"items"')),
    "human": (["x", "x", "comment", "hi", "--author", "cos"],("stdout", "/human/")),
    "human-apply": (["--board", "x"], ("stderr", "capsule human-apply:")),
    "incidents": (["list"], ("stderr", "capsule incidents:")),
    "brainstorm": (["show", "x", "--board", "x"], ("stderr", "capsule brainstorm show: refused")),
    "image": (["render", "--project", "{home}/nope"],
              ("stderr", "capsule image: cannot read")),
    "vault-context": (["show", "{home}/nope.json"],
                      ("stderr", "capsule vault-context: cannot read")),
    "lookout": (["status"], ("stdout", '"shipper_running"')),
    "status": (["--json"], ("stdout", '"outside_factory"')),
    "board": (["--once"], ("stderr", "no board named")),
    "intake": (["x", "--title", "t", "--body-file", "{home}/nope.md"], ("stderr", "capsule intake: --body-file:")),
    "prior": (["t"], ("stdout", "no prior verdicts found")),
    "cos": (["brief"], ("stdout", "CHIEF OF STAFF BRIEF")),
    "outbox-send": (["paused", "x"], ("stderr", "capsule outbox-send:")),
    "pause": (["--reason", "x"], ("stdout", "paused 0 board(s)")),
    "unpark": ([], ("stdout", "unparked 0 board(s)")),
    "card": (["nope", "t_x", "history"], ("stderr", "capsule card: no board")),
    "names": (["backfill", "nope", "--dry-run"], ("stderr", "capsule names: no board")),
    "golden": (["run", "nope"], ("stderr", "capsule golden: no golden cases")),
    "tokens": (["--root", "{home}/nope"], ("stderr", "capsule tokens: no such directory")),
    "auditor": (["packet", "--board", "nope"], ("stdout", '"schema": "audit_packet.v1"')),
    "loophole": (["{home}/nope.jsonl"], ("stderr", "capsule loophole: cannot read")),
    "host": (["get", "nope"], ("stderr", "capsule host: unknown key")),
    "blind": (["preflight", "nope"], ("stderr", "capsule blind preflight:")),
    "doctor": (["--offline"], ("stdout", "doctor: ")),
    "rhythm": (["no-such-board", "--at", "2026-11-09T10:00"],
               ("stderr", "capsule rhythm: no board")),
    # a dry run (changes nothing) with a temp HOME; off macOS the installer prints its pointer
    "install": (["--dry-run"], ("stdout", "WOULD capsule doctor")
                if platform.system() == "Darwin" else ("stderr", "macOS-only")),
    "threat-model": (["check", "--repo", "{home}"], ("stdout", "file missing")),
    "pr": (["daily", "nope", "--dry-run"], ("stderr", "capsule pr daily:")),
    "publish": (["--capsule-home", "{home}"], ("stdout", "capsule publish: off")),
    "tick-watch": (["--capsule-home", "{home}"], ("stdout", "tick-watch: no boards")),
    "veto": (["nope", "ad_0000"], ("stderr", "capsule veto: no board")),
    "stalls": (["--capsule-home", "{home}"], ("stderr", "capsule stalls: no board file")),
    "patrol": (["--capsule-home", "{home}"], ("stderr", "capsule patrol: no board file")),
    "why": (["--capsule-home", "{home}"], ("stderr", "capsule why: no board file")),
    "ask-gate": (["check", "nope", "{home}/nope.json", "--capsule-home", "{home}"],
                 ("stderr", "capsule ask-gate:")),
    "plan": (["check", "{home}/nope.md"], ("stderr", "capsule plan: no such plan file")),
    "security": (["check", "--repo", "{home}", "--findings", "{home}/nope.json"],
                 ("stderr", "not readable JSON")),
    "design": (["reaches-owner", "--files", "-1", "--reversible", "yes"],
               ("stderr", "--files must be 0 or more")),
}
# doctor probes real tools: point its PATH and docker dir at a folder that has none, so this
# test never reaches the machine's docker, claude or gh (it just reports them MISSING)
EXTRA_ENV = {"doctor": {"PATH": "{home}/nobin", "AGENT_OPS_DOCKER_BIN": "{home}/nobin"},
             "install": {"PATH": "{home}/nobin", "AGENT_OPS_DOCKER_BIN": "{home}/nobin",
                         "HOME": "{home}/user"}}
# subcommands with no --capsule-home
NO_CAPSULE_HOME_FLAG = {"run-one", "tokens", "loophole", "image", "threat-model", "plan",
                        "security", "design", "vault-context"}


def _parser_subcommands():
    """The real subcommand set, read from `--help`'s own usage line - a subprocess call, never
    a direct import of `capsule.__main__` (see the module docstring)."""
    r = subprocess.run([sys.executable, "-m", "capsule", "--help"], cwd=paths.REPO,
                       capture_output=True, text=True, timeout=30)
    m = re.search(r"\{([\w,-]+)\}", r.stdout)
    return set(m.group(1).split(",")) if m else set()


class Dispatch(unittest.TestCase):
    def test_board_tick_dispatches_to_its_own_handler(self):
        """`board tick` is a verb inside `board`, so it has no INVOCATIONS row of its own: a dry
        run with a temp HOME must reach the tick-setup handler (its WOULD lines), write nothing."""
        home = Path(tempfile.mkdtemp())
        (home / "boards" / "demo").mkdir(parents=True)
        (home / "boards" / "demo" / "config.json").write_text("{}\n")
        env = {**os.environ, "HOME": str(home / "user"),
               "GIT_ALLOW_PROTOCOL": "file"}     # a broken dry run can never clone off-box
        r = subprocess.run([sys.executable, "-m", "capsule", "board", "tick", "demo",
                            "--dry-run", "--capsule-home", str(home)], cwd=paths.REPO,
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r)
        self.assertIn("WOULD write", r.stdout)
        self.assertIn("capsule-tick-demo.plist", r.stdout)
        self.assertEqual(sorted(p.name for p in home.iterdir()), ["boards"])
        self.assertFalse((home / "user").exists())

    def test_cos_ask_dispatches_to_its_own_handler(self):
        """`cos` has two verbs, one INVOCATIONS row (`brief`); `ask` reaches its own handler and
        refuses a board that does not exist."""
        home = Path(tempfile.mkdtemp())
        r = subprocess.run([sys.executable, "-m", "capsule", "cos", "ask", "nope", "t_x",
                            "--title", "t", "--what", "w", "--why", "y", "--if-yes", "a",
                            "--if-no", "b", "--pick", "Yes", "--because", "c",
                            "--capsule-home", str(home)], cwd=paths.REPO,
                           capture_output=True, text=True, timeout=30)
        self.assertIn("capsule cos ask: refused", r.stderr, r)

    def test_invocations_cover_every_subcommand(self):
        self.assertEqual(set(INVOCATIONS), _parser_subcommands())

    def test_every_subcommand_dispatches_to_its_own_handler(self):
        home = Path(tempfile.mkdtemp())
        for cmd, (extra, (stream, marker)) in INVOCATIONS.items():
            argv = [a.format(home=home) for a in extra]
            if cmd not in NO_CAPSULE_HOME_FLAG:
                argv += ["--capsule-home", str(home)]
            extra = {k: v.format(home=home) for k, v in EXTRA_ENV.get(cmd, {}).items()}
            env = {**os.environ, **extra}
            r = subprocess.run([sys.executable, "-m", "capsule", cmd, *argv], cwd=paths.REPO,
                               capture_output=True, text=True, timeout=30, env=env)
            self.assertIn(marker, getattr(r, stream), f"{cmd}: {r!r}")


class VersionGuard(unittest.TestCase):
    """A stock Mac's python3 is 3.9: `-m capsule` must say so in one plain line, not a traceback."""
    MAIN = paths.REPO / "capsule" / "__main__.py"

    def test_an_old_python_gets_one_plain_line_naming_3_11_and_install_sh(self):
        code = ("import runpy, sys; sys.version_info = (3, 9, 6, 'final', 0); "
                "runpy.run_module('capsule', run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", code, "doctor"], cwd=paths.REPO,
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0, r)
        self.assertEqual(r.stdout, "")
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertIn("3.11", r.stderr)
        self.assertIn("./install.sh", r.stderr)
        self.assertIn("~/.local/bin/capsule", r.stderr)      # the route that works on a stock Mac
        self.assertNotIn("Traceback", r.stderr)

    def test_the_file_parses_as_python_3_9(self):
        import ast
        tree = ast.parse(self.MAIN.read_text(), feature_version=(3, 9))     # raises on newer syntax
        self.assertIsInstance(tree, ast.Module)

    def test_the_guard_runs_before_any_capsule_import(self):
        text = self.MAIN.read_text()
        self.assertLess(text.index("sys.version_info < (3, 11)"), text.index("from capsule import"))


if __name__ == "__main__":
    unittest.main()
