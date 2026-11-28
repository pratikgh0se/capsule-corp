"""`python3 -m harness.unittest_runner --card <id> [--out /out] [--list] <start_dir>`: run one
card's unittest tests and report them for the tick's two oracles (blind spec 3.3, 4.2). Run it as
`cd /blind && exec python3 -m harness.unittest_runner ...`: the cwd must be the snapshot, never the
product's workspace, because `-m` puts the cwd first on sys.path, and `exec` leaves no shell
behind (see the paragraph on the dumpable flag below). A blind repo made before `exec` was added
keeps its old line: `capsule blind init` never rewrites an existing `blind.toml`, so edit it.

The run is a plain `unittest.TextTestRunner` with no TestResult subclass; each test's `run` is
only wrapped to note that it ran and what the product showed it. unittest's own output goes to a
buffer and only the summary (the `Ran N tests` line and the final status line) reaches stdout (what
a test or unittest prints is dropped), so no traceback or assertion text leaves the container.
`<out>/results.jsonl` is built after the run from the standard result lists: one row
`{id, result, observed?}` per test that ran (id `<card_id>.<test method name>`) plus one row per
setUpClass/setUpModule holder.

The contract the two oracles share:
- rows = `Ran N` tests plus the holder rows (a class whose setup failed or skipped runs no test).
- any `fail` or `error` row  iff  the status line is FAILED  iff  the exit code is not 0.
  (A failing subtest, or a failure plus a tearDown error, is one row: the worst outcome wins.)
- `skipped` rows <= the status line's `skipped=`: a skip inside a subTest counts once in the status
  line but a test is one row, so several subTest skips make one row, and a test that also failed is
  a `fail` row. (An expected failure is a `pass` row; an unexpected success is a `fail` row.)
Test names must be unique within a card; a duplicate is harness misuse (exit 2, nothing run).

Run as a program on Linux, the runner first clears its dumpable flag (`prctl(PR_SET_DUMPABLE, 0)`).
That closes the runner's memory and file descriptors to other processes: a process of the code
under test, same user, cannot read the runner's `/proc/<pid>/environ`, `mem` or `fd`. Run it with
`exec` (the run line does), so the runner is the process the shell started and no same-user shell
stays as its parent, holding its stdout pipe and setting the exit code the second oracle reads.
Every `execve` resets the flag, so the product's own subprocesses read their own `/proc/self`
as usual. If the flag cannot be cleared the run is a failed setup (exit 2, nothing run, no results
file), never a pass. Elsewhere it does nothing.

What this does not close: product code a test imports runs inside the runner; the test files in
/blind stay readable; /out stays writable; same-user signals still work (a killed runner reads as
an inconclusive run); `run()` hands the product its environment; code in the runner's own process,
and a forked child that never execs, get a permission error on their own `/proc/self/environ` and
`/proc/self/mem`."""
import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

from . import api

PR_SET_DUMPABLE, PR_GET_DUMPABLE = 4, 3     # <linux/prctl.h>
ID_CHARS = 200            # blind_result.v1 caps a test id at 200 characters
RANK = ("pass", "skipped", "fail", "error")      # the worst outcome of one test wins


def _protect():
    """None when the runner's memory and environment are closed to same-user processes (Linux) or
    nothing is asked of it (any other system); else why the flag could not be cleared. The flag
    is read back: a set that did not stick is a failure."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        call = ctypes.CDLL(None, use_errno=True).prctl
    except (ImportError, OSError, AttributeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    zero = ctypes.c_ulong(0)
    if call(PR_SET_DUMPABLE, zero, zero, zero, zero) != 0:
        return f"prctl(PR_SET_DUMPABLE) failed (errno {ctypes.get_errno()})"
    if call(PR_GET_DUMPABLE, zero, zero, zero, zero) != 0:
        return "the dumpable flag is still set after prctl(PR_SET_DUMPABLE, 0)"
    return None


@contextlib.contextmanager
def _silenced():
    """Drop what the block writes to stdout or stderr (test modules print at import)."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _name(test):
    """The test's own method name (for a module that failed to import, its module name)."""
    return getattr(test, "_testMethodName", None) or test.id().rsplit(".", 1)[-1]


def _watch(test, seen):
    """Record, as `test` runs, that it ran and the product output it observed."""
    plain = test.run

    def run(result=None):
        api.take_observed()
        try:
            return plain(result)
        finally:
            seen[test.id()] = (_name(test), api.take_observed())
    test.run = run


def _outcomes(result):
    """({test id: worst outcome}, [(holder id, outcome)]) from the standard result lists."""
    worst, holders = {}, []
    entries = [(t, "skipped") for t, _ in result.skipped]
    entries += [(t, "fail") for t, _ in result.failures]
    entries += [(t, "error") for t, _ in result.errors]
    entries += [(t, "fail") for t in result.unexpectedSuccesses]
    for test, outcome in entries:
        case = getattr(test, "test_case", test)         # a subtest reports against its parent
        if isinstance(case, unittest.TestCase):
            worst[case.id()] = max(worst.get(case.id(), "pass"), outcome, key=RANK.index)
        else:
            holders.append((test.id(), outcome))
    return worst, holders


def _rows(card, result, seen):
    worst, holders = _outcomes(result)
    rows = []
    for test_id, (name, observed) in seen.items():
        row = {"id": f"{card}.{name}"[:ID_CHARS], "result": worst.get(test_id, "pass")}
        rows.append({**row, "observed": observed} if observed else row)
    return rows + [{"id": f"{card}.{hid}"[:ID_CHARS], "result": out} for hid, out in holders]


def _summary(text):
    """The `Ran N tests` line and the final status line of unittest's own output."""
    lines = [x for x in text.splitlines() if x.strip()]
    ran = next((x for x in reversed(lines) if x.startswith("Ran ")), "Ran 0 tests")
    return f"{ran}\n\n{lines[-1] if lines else 'FAILED'}"


def _run(card, tests, out):
    seen, stream = {}, io.StringIO()
    for test in tests:
        _watch(test, seen)
    # buffer=True keeps a passing test's prints, but on a failure unittest copies them to the
    # original stdout: that original is a sink here, so only `print(_summary())` reaches the tick
    with _silenced():
        result = unittest.TextTestRunner(stream=stream, verbosity=0, buffer=True).run(
            unittest.TestSuite(tests))
    out.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(row) + "\n" for row in _rows(card, result, seen))
    (out / "results.jsonl").write_text(body, encoding="utf-8")
    print(_summary(stream.getvalue()))
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness.unittest_runner")
    ap.add_argument("start_dir")
    ap.add_argument("--card", required=True)
    ap.add_argument("--out", default="/out")
    ap.add_argument("--list", action="store_true", help="print the test ids; run nothing")
    args = ap.parse_args(argv)
    with _silenced():       # importing the test files runs their top-level code
        suite = unittest.defaultTestLoader.discover(args.start_dir, top_level_dir=args.start_dir)
    tests = list(_flatten(suite))
    names = [_name(t) for t in tests]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        print(f"harness: duplicate test names in card {args.card}: {', '.join(dupes)}",
              file=sys.stderr)
        return 2
    if args.list:
        print("\n".join(f"{args.card}.{n}" for n in names))
        return 0
    return _run(args.card, tests, Path(args.out))


if __name__ == "__main__":
    if (why := _protect()) is not None:        # before any test file is imported
        print(f"harness: cannot protect the runner: {why}", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
