#!/usr/bin/env python3
"""Run the mutation gate (its own pass includes the simplicity check, Z8) on the working tree.

The gate compares two commits, so the tree (uncommitted and untracked files included) is
committed in a throwaway shared clone: this repo's branch, HEAD, index and refs stay as they
are. The clone is deleted on exit. Usage: bin/precheck-gates.py [--base <rev>] [--repo .]
Exit 0 when both gates pass, 1 when one fails."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile


# The worker env points git at the real git dir (claude.py sets these); scratch calls must not.
GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")
GATE_TIMEOUT = 600    # matches workspace.GIT_SLOW_TIMEOUT
CLEAN_ENV = {k: v for k, v in os.environ.items() if k not in GIT_ENV}


def git(cwd, *args, env=None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if p.returncode:
        raise SystemExit(f"precheck-gates: git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def scratch_git(scratch, *args):
    return git(scratch, *args, env=CLEAN_ENV)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="HEAD", help="revision the range starts from")
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    repo = os.path.abspath(a.repo)
    base = git(repo, "rev-parse", a.base)
    scratch = tempfile.mkdtemp(prefix="precheck-gates-")
    try:
        # Clone `repo` into `scratch` (objects shared), then commit its working tree there.
        source = git(repo, "rev-parse", "--absolute-git-dir")    # honours an inherited GIT_DIR
        scratch_git(repo, "clone", "-q", "--shared", "--no-checkout", source, scratch)
        names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                    "--", ".", ":(exclude,top).worker-exec")    # the review gate's SCRATCH_EXCLUDE
        for name in filter(None, names.split("\0")):
            src, dst = os.path.join(repo, name), os.path.join(scratch, name)
            if os.path.islink(src) or os.path.isfile(src):    # a deleted file stays deleted; a
                os.makedirs(os.path.dirname(dst), exist_ok=True)    # nested repo (dir) is skipped
                shutil.copy2(src, dst, follow_symlinks=False)
        scratch_git(scratch, "add", "-A")
        scratch_git(scratch, "-c", "user.email=precheck@localhost", "-c", "user.name=precheck",
                    "commit", "-q", "--allow-empty", "-m", "precheck snapshot")
        head = scratch_git(scratch, "rev-parse", "HEAD")
        cmd = [sys.executable, os.path.join(scratch, "tests", "mutation_check.py"),
               "--base", base, "--head", head, "--repo", scratch]
        print(f"== mutation_check.py --base {base[:10]} (working tree)", flush=True)
        try:
            return int(subprocess.run(cmd, cwd=scratch, env=CLEAN_ENV,
                                      timeout=GATE_TIMEOUT).returncode != 0)
        except subprocess.TimeoutExpired:
            print(f"precheck-gates: mutation_check.py timed out after {GATE_TIMEOUT}s")
            return 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
