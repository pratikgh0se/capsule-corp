"""Korin's blind recurrence tests (D161/D17): a `blind:<group>/<file>::<Class>.<test>` id names a
test in the private tests repo (`korin.blind_tests`). Nobody reads it; the gate learns only pass,
fail, the ran count and the tests repo HEAD. Test output is never returned or logged."""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BLIND_RE = re.compile(r"^blind:([A-Za-z_][A-Za-z0-9_-]*)/([A-Za-z_][A-Za-z0-9_]*)"
                      r"::([A-Za-z_][A-Za-z0-9_]*)\.(test_[A-Za-z0-9_]+)$")
BLIND_SHAPE = r"blind:<group>/<file>::<Class>.<test>"
UNSET = (f"recurrence test refused: korin.blind_tests is not set"
         f" (the tests repo path for {BLIND_SHAPE} ids)")


def main_head(checkout):
    """The sha `main` points at in `checkout`, or None (not a git repo, no main)."""
    return head(checkout, "main")


def tests_head(repo):
    return head(repo, "HEAD")


def head(repo, rev):
    """The sha `rev` names in the repo, or None."""
    if not repo:
        return None
    try:
        res = subprocess.run(["git", "-C", str(Path(repo).expanduser()), "rev-parse", "--verify",
                              "-q", rev], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() or None if res.returncode == 0 else None


def _extract(checkout, rev, dest):
    try:
        arch = subprocess.Popen(["git", "-C", str(checkout), "archive", rev],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    try:
        tar = subprocess.run(["tar", "-x", "-C", dest], stdin=arch.stdout, capture_output=True,
                             timeout=120)
    except (subprocess.SubprocessError, OSError):
        arch.kill()
        return False
    finally:
        arch.stdout.close()
        arch.wait()
    return arch.returncode == 0 and tar.returncode == 0


SECRETISH = re.compile(r"secret|token|key|passw|credential|auth|cookie", re.I)


def _env(tmp, home):
    """The test's environment: no secret-like variable, a throwaway CAPSULE_HOME, nothing sent."""
    keep = {k: v for k, v in os.environ.items() if not SECRETISH.search(k)}
    return dict(keep, CAPSULE_UNDER_TEST=tmp, CAPSULE_HOME=home, AGENT_OPS_NO_SEND="1",
                PYTHONDONTWRITEBYTECODE="1")


def _tests_problem(repo, group):
    """Why the tests repo cannot back evidence (no HEAD, or changes inside the test's own group
    folder; other groups may be in progress), or None."""
    if not tests_head(repo):
        return "blind tests repo has no readable HEAD"
    try:
        res = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", group],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return "blind tests repo status unreadable"
    if res.returncode != 0 or res.stdout.strip():
        return f"blind tests group {group} has uncommitted changes: evidence HEAD would not match"
    return None


def run(test_id, checkout, rev, tests_repo, timeout):
    """(state, ran, why): state is "pass", "fail" or "error". Error = no run happened or ran < 1
    (not red, not green). `why` never holds the test's own output."""
    m = BLIND_RE.match(test_id or "")
    if not m:
        return "error", 0, f"recurrence test {test_id!r} is not a {BLIND_SHAPE} id"
    if not tests_repo:
        return "error", 0, UNSET
    repo = Path(tests_repo).expanduser()
    if not (repo / m.group(1)).is_dir():
        return "error", 0, f"blind tests group {m.group(1)} is not in the tests repo"
    if (problem := _tests_problem(repo, m.group(1))):
        return "error", 0, problem
    with tempfile.TemporaryDirectory(prefix="korin-blind-") as tmp, \
            tempfile.TemporaryDirectory(prefix="korin-home-") as home:
        if not _extract(Path(checkout).expanduser(), rev, tmp):
            return "error", 0, f"git archive of {rev[:12]} failed in {checkout}"
        env = _env(tmp, home)
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", m.group(1), "-t", ".",
               "-p", f"{m.group(2)}.py", "-k", f"{m.group(3)}.{m.group(4)}"]
        try:
            res = subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True, text=True,
                                 timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return "error", 0, f"blind test {test_id} timed out after {timeout}s"
    found = re.search(r"^Ran (\d+) tests? in", res.stderr or "", re.M)
    ran = int(found.group(1)) if found else 0
    if ran < 1:
        return "error", 0, f"blind test {test_id} at {rev[:12]}: ran 0 tests (an error)"
    return ("pass" if res.returncode == 0 else "fail"), ran, ""


def run_green(test_id, checkout, tests_repo, timeout):
    """(passed, detail) on main."""
    state, ran, why = run(test_id, checkout, "main", tests_repo, timeout)
    sha = (tests_head(tests_repo) or "?")[:12]
    if state != "pass":
        return False, why or f"blind test {test_id} on main: failed (ran {ran}, tests {sha})"
    return True, f"blind test {test_id} passed on main (ran {ran}, tests {sha})"


def is_new(test_id, checkout, open_sha, tests_repo, timeout):
    """(ok, detail): red on the incident's `main_sha`, green on main."""
    if not open_sha:
        return False, f"no main commit recorded when the incident opened: {test_id} is not new"
    red, ran, why = run(test_id, checkout, open_sha, tests_repo, timeout)
    if red != "fail":
        return False, why or (f"blind test {test_id} passed at {open_sha[:12]} (incident open):"
                              " not new")
    ok, detail = run_green(test_id, checkout, tests_repo, timeout)
    if not ok:
        return False, detail
    return True, (f"blind test {test_id} is red at {open_sha[:12]} (ran {ran}) and green on main"
                  f" (tests {(tests_head(tests_repo) or '?')[:12]})")


def blind_kw(incident, tests):
    """Extra keyword for a `blind:` recurrence id only: the tests repo path."""
    return {"blind_tests": tests} if BLIND_RE.match(incident.get("recurrence_test") or "") else {}


def record(conn, incident, tests):
    """Gate evidence: the tests repo HEAD a blind recurrence id passed against."""
    if blind_kw(incident, tests):
        from capsule import incidents
        incidents.attach(conn, incident["id"], [f"blind tests repo HEAD {tests_head(tests)}"])
