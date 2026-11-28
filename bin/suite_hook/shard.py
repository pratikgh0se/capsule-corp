"""capsule-suite --shards worker: `list START OUT_JSON` or `run START RESULT_JSON SECS [MODULE...]`.

Both modes use the same plain discovery as `python3 -m unittest discover -s START`, so a test
is found exactly when plain discovery finds it. `list` writes {"modules": {m: [test id, ...]}};
`run` runs only the tests of the named modules and writes its counts, the ids of the tests that
ended, the skipped or errored class and module fixtures, whether the run was stopped early, the
seconds per module and the failing ids to RESULT_JSON. SECS > 0 makes the worker exit by itself
with a stack dump after that long (its wrapper may be gone). A module key is the dotted module of
the test's class (a module that failed to import is keyed by its own name). Output is
unittest's own. A suite that is not a plain TestSuite (a load_tests with its own run()) would be
flattened away, so it is refused."""
import collections
import faulthandler
import json
import os
import sys
import time
import unittest


def key(test):
    """The module a test belongs to."""
    if type(test).__name__ == "_FailedTest":
        return test.id().split("_FailedTest.", 1)[1]
    return type(test).__module__


def leaves(suite):
    for t in suite:
        if type(t) is unittest.TestSuite:
            yield from leaves(t)
        elif isinstance(t, unittest.TestSuite):
            raise TypeError(f"{type(t).__name__} is not a plain TestSuite; its run() would be lost")
        else:
            yield t


def _timed_result(seconds, ended):
    """A TextTestResult class that charges the time since the last test ended to the module of
    the test that just ended, so class and module fixtures count towards their module; the id
    of every test that ended goes into `ended`."""
    def make(*args, **kw):
        res = unittest.TextTestResult(*args, **kw)
        last, stop = [time.monotonic()], res.stopTest

        def stop_test(test):
            stop(test)
            now = time.monotonic()
            seconds[key(test)] += now - last[0]
            last[0] = now
            ended.append(test.id())
        res.stopTest = stop_test
        return res
    return make


def run(start, result_path, modules):
    keep = unittest.TestSuite(t for t in leaves(unittest.TestLoader().discover(start))
                              if key(t) in modules)
    seconds, ended = collections.Counter(), []
    res = unittest.TextTestRunner(resultclass=_timed_result(seconds, ended), verbosity=0).run(keep)
    held = [t.id() for t, _ in res.skipped + res.errors if type(t).__name__ == "_ErrorHolder"]
    doc = {"ran": res.testsRun, "failures": len(res.failures), "errors": len(res.errors),
           "skipped": len(res.skipped), "expected_failures": len(res.expectedFailures),
           "unexpected_successes": len(res.unexpectedSuccesses), "ids": ended, "fixtures": held,
           "stopped": res.shouldStop, "seconds": seconds,
           "failed": sorted(t.id() for t, _ in res.failures + res.errors)}
    with open(result_path, "w") as f:
        json.dump(doc, f)
    return 0 if res.wasSuccessful() else 1


def main(argv):
    sys.path.insert(0, os.getcwd())  # what `python3 -m unittest` does and `python3 FILE` does not
    if argv[0] == "list":
        found = collections.defaultdict(list)
        for t in leaves(unittest.TestLoader().discover(argv[1])):
            found[key(t)].append(t.id())
        with open(argv[2], "w") as f:
            json.dump({"modules": found}, f)
        return 0
    if float(argv[3]) > 0:
        faulthandler.dump_traceback_later(float(argv[3]), exit=True)
    return run(argv[1], argv[2], set(argv[4:]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
