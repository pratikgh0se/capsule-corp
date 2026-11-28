"""Korin's closure-gate test runners: run a recurrence test on main, and prove it new (row 456)."""
import ast
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from capsule import incident_blind_run, outbox, slack_msgs

# `<pkg>.test_<module>.<Class>.test_<name>` only: never an arbitrary importable callable
_DOTTED_ID = (r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*test_[A-Za-z0-9_]+"
              r"\.[A-Za-z_][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+")
# second form: one shell suite, optionally one `check "<label>"` in it (tests/test_x.sh[::label])
_SHELL_ID = r'tests/test_[A-Za-z0-9_]+\.sh(?:::[^"\\\n]{1,200})?'
SHELL_ID_RE = re.compile(r'^(tests/test_[A-Za-z0-9_]+\.sh)(?:::([^"\\\n]{1,200}))?$')
_BLIND_ID = incident_blind_run.BLIND_RE.pattern[1:-1]
TEST_ID_RE = re.compile(rf"^(?:{_DOTTED_ID}|{_SHELL_ID}|{_BLIND_ID})$")
TEST_TIMEOUT = 300
OUTPUT_TAIL = 4000


def page(conn, fp, task_id, now, headline, *, fields=(), file_body=None):
    """The one page line of a recurrence (REPEAT, RECURRED): `incident` class, one Slack message per
    fingerprint, thread of the card."""
    outbox.notify(conn, "incident", headline, fields=fields, thread_key=task_id,
                  key=slack_msgs.broken_key(fp), page=True, file_body=file_body, now=now)


def recurred(conn, fp, inc, task_id, now, key, text):
    """The RECURRED page of a fix-pending incident: one per fix-pending period (the page line is
    the record), none for a host-wall outage (`wall_unavailable`: the wall notice is its page)."""
    head = f"RECURRED {inc['id']} while fix card {inc['fix_card']} is pending"
    if str(key).startswith("wall_unavailable") or conn.execute(
            "SELECT 1 FROM outbox WHERE channel='file' AND instr(body, ?) > 0", (head,)).fetchone():
        return
    page(conn, fp, task_id, now, f"{head}: {text[:200]}")


def _run_shell_suite(test_id, path, label, checkout, timeout):
    """(passed, detail) for a shell suite: exit 0, >= 1 `PASS ` line, no `FAIL ` line (and, with a
    label, a `PASS <label>` line). A `# needs:` header (docker/host/postgres) is refused."""
    pre = f"recurrence test {test_id}"
    suite = checkout / path
    if not suite.is_file():
        return False, f"{pre}: {path} is not on main"
    head = suite.read_text(encoding="utf-8", errors="replace").splitlines()[:30]
    needs = next((ln.strip() for ln in head if re.match(r"^#\s*needs:", ln)), None)
    if needs:
        return False, f"{pre} refused: the gate cannot guarantee its environment ({needs})"
    with tempfile.TemporaryDirectory(prefix="korin-home-") as home:
        env = dict(os.environ, CAPSULE_HOME=home, PYTHONDONTWRITEBYTECODE="1")
        try:
            res = subprocess.run(["bash", path], cwd=str(checkout), env=env, capture_output=True,
                                 text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, f"{pre} timed out after {timeout}s on main"
    out = (res.stdout or "") + (res.stderr or "")
    lines = [ln.rstrip() for ln in (res.stdout or "").splitlines()]
    passes = [ln for ln in lines if ln.startswith("PASS ")]
    fails = [ln for ln in lines if ln.startswith("FAIL ")]
    label_missing = bool(label) and f"PASS {label}" not in passes
    if res.returncode != 0 or not passes or fails or label_missing:
        why = "label not passed" if label_missing else "suite not clean"
        tail = out[-OUTPUT_TAIL:].strip().replace("\n", " | ")[-600:]
        return False, (f"{pre} on main: {why}: exit {res.returncode}, {len(passes)} PASS,"
                       f" {len(fails)} FAIL: {tail}")
    return True, f"{pre} passed on main ({len(passes)} PASS lines)"


def run_recurrence_test(test_id, checkout, *, timeout=TEST_TIMEOUT, blind_tests=None):
    """(passed, detail). Refuses unless `checkout` is on `main`; runs the one test with a throwaway
    CAPSULE_HOME (never the live one), no bytecode writes; passed = exit 0 AND "Ran N", N >= 1."""
    checkout = Path(checkout).expanduser()
    if not TEST_ID_RE.match(test_id or ""):
        return False, (f"recurrence test {test_id!r} is not a dotted unittest id"
                       " or a tests/test_<name>.sh[::label] id or a"
                       f" {incident_blind_run.BLIND_SHAPE} id (the group may hold hyphens)")
    try:
        branch = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"main checkout {checkout} unreadable: {exc}"
    if branch != "main":
        return False, f"main checkout {checkout} is on {branch or 'no branch'!r}, not main"
    if incident_blind_run.BLIND_RE.match(test_id):
        return incident_blind_run.run_green(test_id, checkout, blind_tests, timeout)
    shell = SHELL_ID_RE.match(test_id)
    if shell:
        return _run_shell_suite(test_id, shell.group(1), shell.group(2), checkout, timeout)
    with tempfile.TemporaryDirectory(prefix="korin-home-") as home:
        env = dict(os.environ, CAPSULE_HOME=home, PYTHONDONTWRITEBYTECODE="1")
        try:
            res = subprocess.run([sys.executable, "-m", "unittest", test_id], cwd=str(checkout),
                                 env=env, capture_output=True, text=True, timeout=timeout,
                                 stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, f"recurrence test {test_id} timed out after {timeout}s on main"
    out = (res.stdout or "") + (res.stderr or "")
    m = re.search(r"^Ran (\d+) tests? in", out, re.M)
    ran = int(m.group(1)) if m else 0
    if res.returncode != 0 or ran < 1:
        return False, (f"recurrence test {test_id} on main: exit {res.returncode}, ran {ran}: "
                       + out[-OUTPUT_TAIL:].strip().replace("\n", " | ")[-600:])
    return True, f"recurrence test {test_id} passed on main (ran {ran})"


def _file_at(checkout, rev, path):
    try:
        res = subprocess.run(
            ["git", "-C", str(Path(checkout).expanduser()), "show", f"{rev}:{path}"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


FILE_CAP, ROOM = 800, 150   # tests/size_check.py FILE_LIMIT; a recurrence module needs ROOM free


def recurrence_problems(test_id, checkout, rev="main", mod=None):
    """[] or the reasons a recurrence test id is refused (inc_2185fb86): not a test id, or a
    dotted id whose module at `rev` has no room (a shell id is not measured by the size check)."""
    if not TEST_ID_RE.fullmatch(test_id):
        return [f"recurrence_test {test_id!r} is not a dotted unittest id"
                " or a tests/test_<name>.sh[::<label>] id or a"
                f" {incident_blind_run.BLIND_SHAPE} id (the group may hold hyphens)"]
    if SHELL_ID_RE.match(test_id) or incident_blind_run.BLIND_RE.match(test_id):
        return []
    mod = mod or "/".join(test_id.split(".")[:-2]) + ".py"
    size = len((_file_at(checkout, rev, mod) or "").splitlines())
    if size <= FILE_CAP - ROOM:
        return []
    return [f"recurrence_test module {mod} has {size} lines on {rev[:12]}"
            f" (cap {FILE_CAP}, room {ROOM}): name a new test module"]


def _shell_is_new(test_id, shell, checkout, open_sha, rev="main"):
    """`recurrence_test_is_new` for a `tests/test_x.sh[::label]` id."""
    spath, label = shell.group(1), shell.group(2)
    needle = f'check "{label}"'

    def shell_has(rev):
        text = _file_at(checkout, rev, spath)
        if text is None:
            return False
        return needle in text if label else True
    if shell_has(open_sha):
        what = f"check {label!r} in {spath}" if label else spath
        return False, (f"recurrence test {test_id} ({what}) was already on main"
                       f" at {open_sha[:12]} (incident open): not new")
    if not shell_has(rev):
        return False, f"recurrence test {test_id} is not on {rev} ({spath})"
    return True, f"recurrence test {test_id} is new on {rev} since {open_sha[:12]}"


def _defining_path(checkout, rev, path, cls):
    """`path`, or the one sibling test module that `path` imports `cls` from (one hop only), at
    `rev`; `path` when the class is defined in it or no import line names it."""
    text = re.sub(r"#[^\n]*", "", _file_at(checkout, rev, path) or "")
    if re.search(rf"^\s*class {re.escape(cls)}\b", text, re.M):
        return path
    imp = r"^from\s+(\.|[\w.]+\.)(test_\w+)\s+import\s+(\([^)]*\)|[^\n]*)"
    for m in re.finditer(imp, text, re.M):
        names = m.group(3).strip("() \n").replace("\n", ",").split(",")
        if cls in [n.split()[0] for n in names if n.split()]:
            pkg = m.group(1).rstrip(".").replace(".", "/")
            base = os.path.dirname(path) if m.group(1) == "." else pkg
            return f"{base}/{m.group(2)}.py".lstrip("/")
    return path


def _defines(text, cls, method, unparsable=False):
    """True when `text` defines class `cls` holding `def method` (the parser, so a `def` line that
    wraps over two lines still counts). A module that does not parse gives `unparsable`: False for
    "is it there now", True for "was it there at the base" (fail closed)."""
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError):
        return unparsable
    return any(isinstance(c, ast.ClassDef) and c.name == cls and any(
        isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == method
        for f in c.body) for c in ast.walk(tree))


def recurrence_test_is_new(test_id, checkout, open_sha, *, blind_tests=None, timeout=TEST_TIMEOUT,
                           rev="main"):
    """(ok, detail): the test (its class AND method in its module file) was NOT on main at the
    commit main pointed at when the incident opened, and IS on `rev` (default main) now - so it was
    written for this fix (row 456), not an old passing test borrowed as a recurrence test."""
    if not TEST_ID_RE.match(test_id or ""):
        return False, f"recurrence test {test_id!r} is not a test_<module>.<Class>.test_<name> id"
    if not open_sha:
        return False, (f"no main commit recorded when the incident opened: {test_id}"
                       " cannot be proven new")
    if incident_blind_run.BLIND_RE.match(test_id):
        return incident_blind_run.is_new(test_id, checkout, open_sha, blind_tests, timeout)
    parts = test_id.split(".")
    cls, method, named = parts[-2], parts[-1], "/".join(parts[:-2]) + ".py"
    shell = SHELL_ID_RE.match(test_id)
    path = named if shell else _defining_path(checkout, rev, named, cls)
    if path != named:
        problems = recurrence_problems(test_id, checkout, open_sha, path)
    else:
        problems = recurrence_problems(test_id, checkout, open_sha)
    if problems:
        return False, problems[0]
    if shell:
        return _shell_is_new(test_id, shell, checkout, open_sha, rev)

    def has(rev, where, unparsable=False):
        return _defines(_file_at(checkout, rev, where), cls, method, unparsable)
    at_open = {named, path, _defining_path(checkout, open_sha, named, cls)}
    if any(has(open_sha, w, True) for w in at_open):
        return False, (f"recurrence test {test_id} was already on main at {open_sha[:12]}"
                       " (incident open): not new")
    if not has(rev, path):
        return False, f"recurrence test {test_id} is not on {rev} ({path})"
    return True, f"recurrence test {test_id} is new on {rev} since {open_sha[:12]}"
