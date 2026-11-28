"""Shared fixture for the blind_mutation suites: temp git repos and a fake `run` (never docker)."""
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AGENT_OPS_NO_SEND", "1")
import blind_mutation as bm  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CALC = ("def add(a, b):\n    return a + b\n\n\ndef is_pos(x):\n    return x > 0\n\n\n"
        "def flag():\n    return True\n")
PROJECT = {"calc.py": CALC,
           "tests/test_calc.py": "def t(a, b):\n    return a == b\n",
           "test_helper.py": "def t(a, b):\n    return a == b\n",
           "x_test.py": "def t(a, b):\n    return a == b\n",
           "broken.py": "def f(:\n    return True\n",
           "latin.py": b"# caf\xe9\ndef f():\n    return True\n",
           "notes.txt": "return True\n"}
ONE, TWO, RED, BROKEN, FIVE = "t_00000001", "t_00000002", "t_00000003", "t_00000004", "t_00000005"
TODAY = "2026-11-02"


def git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def make_repo(root, files):
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "x")
    return root


def blind_files(*cards):
    files = {"blind.toml": "version = 1\n"}
    for card in cards:
        files[f"cards/{card}/test_it.py"] = "assert True  # SECRET TEST SOURCE\n"
    return files


def load(tree):
    return {k: v for k, v in runpy.run_path(str(tree / "calc.py")).items() if callable(v)}


def _weak(f):
    return f["add"](2, 3) == 5


CHECKS = {
    ONE: _weak,
    FIVE: _weak,
    TWO: lambda f: (f["add"](2, 3) == 5 and f["is_pos"](1) is True and f["is_pos"](0) is False
                    and f["flag"]() is True),
    RED: lambda f: False,
    BROKEN: lambda f: None,
}


def fake_run(tree, card_dir):
    assert Path(card_dir).is_dir()
    return CHECKS[Path(card_dir).name](load(tree))


def is_pristine(tree):
    return (tree / "calc.py").read_text() == CALC


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bm-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.project = make_repo(self.tmp / "project", PROJECT)
        self.blind = make_repo(self.tmp / "blind", blind_files(ONE, TWO))
        self.out = self.tmp / "out"

    def score(self, run=fake_run, **kw):
        return bm.score(self.project, git(self.project, "rev-parse", "HEAD"), self.blind,
                        self.out, run=run, today=TODAY, **kw)

    def add_cards(self, *cards):
        for rel, body in blind_files(*cards).items():
            path = self.blind / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        git(self.blind, "add", "-A")
        git(self.blind, "commit", "-q", "-m", "more")
