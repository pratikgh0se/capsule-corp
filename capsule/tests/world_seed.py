"""Tests only: the git world of test_lander.World, built once per process and copied per test.

Each test used to run nine git processes to make a bare remote, a dev clone on `epic/x` with one
`init` commit on `main`, and (for the landing tests) a release clone: ~0.2 s of cpu of a ~1 s test.
`copy_into` builds that world once under the suite's temp root and copies it, so every test still
owns a private world; only the repo and release clones' `origin` url is re-pointed at its copy of
the remote. The index's cached stat data does not survive a copy, which git simply re-checks."""
import shutil
import subprocess
import tempfile
from pathlib import Path

from capsule.tests import tmpclean

BRANCH = "epic/x"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
_seeds = {}


def run(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True).stdout.strip()


def build(root, release):
    remote, repo = root / "remote.git", root / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    run(repo, "checkout", "-q", "-b", "main")
    (repo / "README.md").write_text("main\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "init")
    run(repo, "push", "-q", "origin", "main")
    run(repo, "checkout", "-q", "-b", BRANCH)
    if release:
        subprocess.run(["git", "clone", "-q", str(remote), str(root / "release")], check=True,
                       capture_output=True)


def copy_once(dest, build):
    """`dest` becomes a copy of the dir `build(path)` makes (run once per process, then copied)."""
    if build not in _seeds:
        _seeds[build] = Path(tempfile.mkdtemp(prefix="seed-", dir=tmpclean.install())) / "dir"
        build(_seeds[build])
    shutil.copytree(_seeds[build], dest, symlinks=True)


def copy_into(root, release=False):
    """`root` gets remote.git, repo (and release): the same world `build` makes, private to it."""
    if release not in _seeds:
        _seeds[release] = Path(tempfile.mkdtemp(prefix="world-seed-", dir=tmpclean.install()))
        build(_seeds[release], release)
    seed = _seeds[release]
    for name in ("remote.git", "repo") + (("release",) if release else ()):
        shutil.copytree(seed / name, root / name, symlinks=True)
    for name in ("repo", "release")[:1 + release]:
        conf = root / name / ".git" / "config"
        text = conf.read_text()
        if str(seed / "remote.git") not in text:
            raise RuntimeError(f"{conf} does not point at the seed remote")
        conf.write_text(text.replace(str(seed / "remote.git"), str(root / "remote.git")))
