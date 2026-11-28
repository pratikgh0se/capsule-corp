"""Run one fixed share of a test folder: `python3 -m capsule.test_shard K N [-s DIR] [-t TOP]`.

The test modules under DIR (`test*.py`, discover's default pattern, recursive) are sorted by
dotted name and part K of N (1-based) takes every Nth from offset K-1, so parts 1..N cover each
module exactly once. Exit 0 when the share passes; 1 when any test fails, when DIR holds no test
module at all (a moved or missing folder must not pass), or when a non-empty share ran no test."""
import argparse
import sys
import unittest
from pathlib import Path


def share(start, top, k, n):
    """(part `k` of `n` (1-based), all) of the sorted dotted names of the `test*.py` modules
    under `start`, relative to `top`."""
    names = sorted(".".join(p.relative_to(top).with_suffix("").parts)
                   for p in Path(start).rglob("test*.py"))
    return names[k - 1::n], names


def main(argv=None):
    ap = argparse.ArgumentParser(prog="capsule.test_shard")
    ap.add_argument("k", type=int)
    ap.add_argument("n", type=int)
    ap.add_argument("-s", "--start", default=".")
    ap.add_argument("-t", "--top", default=".")
    args = ap.parse_args(argv)
    if not 1 <= args.k <= args.n:
        ap.error("K must be 1..N")
    names, every = share(args.start, args.top, args.k, args.n)
    print(f"part {args.k}/{args.n}: selected {len(names)} of {len(every)} test modules")
    if not every:
        print(f"FAIL: no test module under {args.start}")
        return 1
    suite = unittest.TestLoader().loadTestsFromNames(names)
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    ok = bool(res.wasSuccessful() and (res.testsRun or not names))
    if names and not res.testsRun:
        print("FAIL: the share ran no test")
    # stderr last: the saved output keeps only the tail of stdout then stderr
    print(f"part {args.k}/{args.n}: ran {len(names)} of {len(every)} test modules",
          file=sys.stderr, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
