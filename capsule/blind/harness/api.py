"""`run`, `read`, `tmp`: how a blind test touches the product without seeing its source.

`run` executes a shell command in the workspace (the card's work, mounted read-only in a blind run)
and keeps what it saw as the test's observed record: command, exit code and the tails of stdout and
stderr, never an assertion message or a traceback. The runner reads that record after each test."""
import atexit
import os
import shutil
import subprocess
import tempfile
from collections import namedtuple
from pathlib import Path

WORKSPACE_ENV = "BLIND_WORKSPACE"
TAIL_LINES = 20
TAIL_CHARS = 2000
CMD_CHARS = 1000
TIMEOUT_EXIT = 124      # the exit code timeout(1) uses

Completed = namedtuple("Completed", "cmd exit stdout stderr")

_last = None            # the observed record of the latest run() call of the current test
_scratch = []           # the one directory every tmp() dir lives under, removed at exit


def _workspace():
    return Path(os.environ.get(WORKSPACE_ENV) or "/workspace")


def _tail(text):
    """The last TAIL_LINES lines of `text`, at most TAIL_CHARS characters."""
    return "\n".join((text or "").splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")


def run(cmd, *, stdin=None, env=None, timeout=60):
    """Run the shell command `cmd` in the workspace; returns Completed(cmd, exit, stdout, stderr).
    A command that outlives `timeout` seconds is killed and reports exit 124. The product never
    inherits PYTHONPATH: it must not import the harness or the tests."""
    global _last
    full = {**{k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, **(env or {})}
    try:
        done = subprocess.run(cmd, shell=True, cwd=str(_workspace()), input=stdin, text=True,
                              capture_output=True, errors="replace", timeout=timeout, env=full)
        result = Completed(cmd, done.returncode, done.stdout or "", done.stderr or "")
    except subprocess.TimeoutExpired as exc:
        note = f"\ntimed out after {timeout}s"
        result = Completed(cmd, TIMEOUT_EXIT, _text(exc.stdout), _text(exc.stderr) + note)
    _last = {"cmd": cmd[:CMD_CHARS] or "?", "exit": max(-255, min(255, result.exit)),
             "stdout_tail": _tail(result.stdout), "stderr_tail": _tail(result.stderr)}
    return result


def tmp():
    """A fresh scratch directory (a Path), removed when the process exits."""
    if not _scratch:
        _scratch.append(Path(tempfile.mkdtemp(prefix="blind-")))
        atexit.register(shutil.rmtree, _scratch[0], ignore_errors=True)
    return Path(tempfile.mkdtemp(dir=_scratch[0]))


def read(path):
    """The text of a file under the workspace or a `tmp()` directory; any other path is refused.
    A relative path means relative to the workspace."""
    target = Path(os.path.realpath(_workspace() / path))
    roots = [Path(os.path.realpath(r)) for r in [_workspace(), *_scratch]]
    if not any(target == root or root in target.parents for root in roots):
        raise ValueError(f"read: {path} is outside the workspace and the tmp directories")
    return target.read_text(encoding="utf-8", errors="replace")


def take_observed():
    """The latest run() record since the last call (None when no command ran); clears it."""
    global _last
    taken, _last = _last, None
    return taken
