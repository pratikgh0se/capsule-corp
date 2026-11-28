"""The GitHub CI conclusion for a pushed main sha (LAND-07, row 171): a second red source for
`landing.verify`, because a Linux-only red is invisible to the local suites. Read-only: it asks
`gh`, it never writes anything."""
import argparse
import json
import subprocess
import sys

TIMEOUT = 60
FAILED = ("failure", "timed_out", "startup_failure")


def conclusion(repo_url, sha, gh="gh", branch="main"):
    """`green` | `red` | `pending` | `unknown` for the Verify workflow's runs of `sha` on main.
    gh missing, auth failed, network down, a non-GitHub remote or unparseable output is
    `unknown`: infra, never red (row 508). A run waiting for approval is `pending`."""
    cmd = [gh, "run", "list", "--workflow", "verify.yml", "--branch", branch, "--commit", sha,
           "-R", repo_url, "--json", "conclusion,status"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        runs = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "unknown"
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        return "unknown"
    if any(r.get("conclusion") in FAILED for r in runs):
        return "red"
    if not runs or any(r.get("status") != "completed" or r.get("conclusion") == "action_required"
                       for r in runs):
        return "pending"     # no run yet means GitHub has not registered the push
    return "green" if any(r.get("conclusion") == "success" for r in runs) else "unknown"


def main(argv=None):
    """`python3 -m capsule.ci_watch <repo_url> <sha>`: print the conclusion (the shell check
    against GitHub's own UI status)."""
    ap = argparse.ArgumentParser(description="GitHub CI conclusion of a sha on main.")
    ap.add_argument("repo_url")
    ap.add_argument("sha")
    a = ap.parse_args(argv)
    print(conclusion(a.repo_url, a.sha))
    return 0


if __name__ == "__main__":
    sys.exit(main())
