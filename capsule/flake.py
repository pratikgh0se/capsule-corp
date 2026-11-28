"""S11 (R14): flaky-test reruns for the tests a card ADDED, behind the `flake_reruns` redesign
switch (all switches ship off). The tick adds one contract command per added unittest test to the
validator's gate; each runs the test RUNS times in the same network-none strong container as every
other contract command and prints one `run <i> rc=<exit> <runner's last line>` per run. The host
then decides from two oracles that must agree (per-run exit code, and the runner's own summary
line): all runs pass = fine, a mix = flaky, every run fails = a failing test. A result that cannot
be read (timeout, a summary that disagrees with the exit code, a missing run) is a finding too,
never a pass. Switch off: no command, no read, no line.

Only Python `unittest` tests are listed (a method named `test*` in a top-level class that has a
base, of a `test_*.py` / `*_test.py` file, whose `def` line is added by the diff); other runners
are not rerun. At most MAX_TESTS are rerun, the rest are named in one evidence-only marker
command. The reruns share the validator's /workspace (v1, Z6: reversible), not a fresh container.

Known limits: a decorator-renamed or parametrised test has no such id and reads as unreadable;
pytest-style module functions are not rerun; a base class that other test classes inherit may
fail when run alone."""
import ast
import re
import shlex
from pathlib import Path

from capsule import config, workspace
from capsule.blind import gate

RUNS = 5                 # C1: reruns per added test
RUN_TIMEOUT = 60         # seconds per run; a timeout ends that test's loop (one hung run, 60 s)
MAX_TESTS = 10           # bounds one review's rerun time
PREFIX = ": flake-rerun "
CAP_PREFIX = ": flake-rerun-cap "
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_RUN_LINE = re.compile(r"^run (\d+) rc=(\d+) ?(.*)$", re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _added_lines(diff):
    """{path: {new line numbers}} for every line a unified diff adds (counted hunk by hunk, so a
    body line that looks like a `+++` header is still body)."""
    added, path, old, new, line = {}, None, 0, 0, 0
    for text in diff.splitlines():
        if old <= 0 and new <= 0:             # between hunks: a file header or a hunk header
            if text.startswith("+++ "):
                path = text[6:] if text.startswith("+++ b/") else None
            elif hunk := _HUNK.match(text):
                old, line, new = int(hunk[1] or 1), int(hunk[2]), int(hunk[3] or 1)
            continue
        mark = text[:1]
        if mark == "+" and path:
            added.setdefault(path, set()).add(line)
        old -= mark in ("-", " ", "")
        new -= mark in ("+", " ", "")
        line += mark in ("+", " ", "")
    return added


def _file_tests(ws, path, lines):
    """Dotted unittest ids of the test methods whose `def` line is among `lines`."""
    name, file = Path(path).name, Path(ws) / path
    if not name.endswith(".py") or not (name.startswith("test_") or name.endswith("_test.py")):
        return []
    module = ".".join(Path(path).with_suffix("").parts)
    if not all(part.isidentifier() for part in module.split(".")) or file.is_symlink():
        return []
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError, OSError):
        return []                              # the suite itself fails on a broken file
    return [f"{module}.{cls.name}.{fn.name}"
            for cls in tree.body if isinstance(cls, ast.ClassDef)
            if any(not (isinstance(b, ast.Name) and b.id == "object") for b in cls.bases)
            for fn in cls.body
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and fn.name.startswith("test") and fn.lineno in lines]


def _command(test_id):
    """One sh command (the container runs `sh -lc`): RUNS runs of one test, each bounded by
    RUN_TIMEOUT; exits with the number of runs that did not exit 0."""
    q = shlex.quote(test_id)
    nums = " ".join(str(i) for i in range(1, RUNS + 1))
    # stderr only: the runner's summary is there, and a test's own prints (stdout) must not
    # land after it; `grep` picks the summary line, `printf` because dash's echo eats escapes
    return (f"{PREFIX}{q} ; bad=0; for i in {nums}; do "
            f"o=$(timeout {RUN_TIMEOUT} python3 -m unittest {q} 2>&1 >/dev/null); rc=$?; "
            """last=$(printf '%s\\n' "$o" | grep -E '^(OK|FAILED|NO TESTS RAN)' | tail -n 1 """
            """| cut -c1-80); printf 'run %s rc=%s %s\\n' "$i" "$rc" "$last"; """
            "[ $rc -eq 0 ] || bad=$((bad+1)); [ $rc -eq 124 ] && break; done; exit $bad")


def commands(cfg, ws, base):
    """The rerun commands for the tests added since `base()` (a callable, so a switch that is off
    reads nothing) in clone `ws`: [] with the switch off or nothing added."""
    if not config.redesign(cfg)["flake_reruns"]:
        return []
    added = _added_lines(workspace.diff(ws, base()))
    ids = list(dict.fromkeys(t for path, lines in sorted(added.items())
                             for t in _file_tests(ws, path, lines)))
    cmds = [_command(t) for t in ids[:MAX_TESTS]]
    if len(ids) > MAX_TESTS:
        cmds.append(f"{CAP_PREFIX}{len(ids) - MAX_TESTS} of {len(ids)} added tests were not rerun")
    return cmds


def _finding(test_id, output):
    """One finding line for a rerun's printed output, or None when all RUNS runs passed."""
    runs = [(int(rc), _ANSI.sub("", summary)) for _, rc, summary in _RUN_LINE.findall(output or "")]
    clear = [(rc == 0 and s.startswith("OK")) or (rc != 0 and s.startswith("FAILED"))
             for rc, s in runs]
    if len(runs) != RUNS or not all(clear):    # the two oracles disagree, or a run is missing
        return (f"flake rerun of `{test_id}` unreadable: {sum(clear)} of {RUNS} runs printed a"
                " result whose exit code and runner summary agree (flake rerun, S11)")
    codes = [rc for rc, _ in runs]
    bad = sum(rc != 0 for rc in codes)
    if bad == 0:
        return None
    if bad == RUNS:
        return f"test `{test_id}` failed all {RUNS} reruns, exit codes {codes} (flake rerun, S11)"
    return (f"flaky test `{test_id}`: {RUNS - bad} of {RUNS} reruns passed, exit codes {codes}"
            " (flake rerun, S11)")


def failures(cfg, conn, spool_root, validator_run):
    """One line per added test whose reruns were not all clean passes, read from the validator
    run's job.json (the evidence the validator was given). [] with the switch off or no
    validator run."""
    if validator_run is None or not config.redesign(cfg)["flake_reruns"]:
        return []
    outputs = gate.outputs(spool_root, validator_run) or []
    found = [_finding(o["cmd"].split()[2], o.get("output")) for o in outputs
             if o["cmd"].startswith(PREFIX)]
    return [line for line in found if line]
