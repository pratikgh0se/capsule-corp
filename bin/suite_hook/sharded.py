"""capsule-suite --shards N: the default discovery, split into at most N groups of test modules.

Each group runs in its own child (shard.py) with its own $TMPDIR, session and the lock fd, so the
lock, the whole-run --timeout (exit 124) and the signals cover all of them. The modules named by
SERIAL run alone, in one child, after the parallel groups. Groups are balanced by the seconds each
module took last run (<lock-dir>/<suite>-shards.json), else by test count. The shards' counts are
summed into unittest's own last lines. The run fails (never reads OK) when plain discovery finds
no test, when a test id that discovery listed did not end in any shard (a skipped or errored
class or module fixture accounts for its own tests) or one ended that discovery did not list, when
a shard stopped early or ended without a result."""
import collections
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HOOK = os.path.dirname(os.path.abspath(__file__))
SHARD = os.path.join(HOOK, "shard.py")
GRACE = 5            # a worker's own deadline is this much past the run's, so the wrapper wins
MAX_SECONDS = 1e6    # a stored module time above this is not a time
# Modules that must not share the machine with another shard: each asserts a wall-clock bound,
# or leaves a file in the repo that other modules can see.
# test_restart: its timeout case needs the spawn within 1 s of the test's own clock read; it
# failed in sharded run 3 at load 69 and, run alone, 1 time in 3 at load 52.
# test_intent writes contracts/unblocked.md; test_unblocked and test_lookout_ship time real
# subprocesses and a local HTTP server.
SERIAL = ("test_claims", "test_hollow_tests", "test_intent", "test_lookout", "test_lookout_ship",
          "test_preflight", "test_restart", "test_unblocked", "test_unblocked_locked_board")
SERIAL_MAINTENANCE = ("test_board_query", "test_decisions")
FIXTURE = re.compile(r"(?:setUpModule|setUpClass) \((.*)\)$")


class _Killed(Exception):
    def __init__(self, signum):
        self.signum = signum


def _terminated(signum, frame):
    raise _Killed(signum)


def _child_env(**extra):
    path = os.pathsep.join(p for p in (HOOK, os.environ.get("PYTHONPATH")) if p)
    return dict(os.environ, PYTHONFAULTHANDLER="1", PYTHONPATH=path, **extra)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _seconds(path):
    """The stored seconds per module; anything that is not a sane finite number is dropped."""
    doc = _load_json(path)
    return {m: float(v) for m, v in (doc.items() if isinstance(doc, dict) else ())
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= MAX_SECONDS}


def _partition(counts, seconds, serial, n):
    """(groups, serial modules): heaviest first onto the lightest of at most n groups."""
    seq = sorted(m for m in counts if m.rsplit(".", 1)[-1] in serial)
    known = [m for m in counts if m in seconds]
    rate = sum(seconds[m] for m in known) / max(1, sum(counts[m] for m in known)) or 1.0

    def weight(m):
        return seconds.get(m, counts[m] * rate)
    rest = sorted((m for m in counts if m not in seq), key=lambda m: (-weight(m), m))
    groups = [SimpleNamespace(load=0.0, modules=[]) for _ in range(min(n, len(rest)))]
    for m in rest:
        g = min(groups, key=lambda g: g.load)
        g.modules.append(m)
        g.load += weight(m)
    return [g.modules for g in groups], seq


def _launch(label, modules, ctx):
    """One shard child: own session, own $TMPDIR, the lock fd, output into its own log."""
    d = os.path.join(ctx.root, f"shard-{label}")
    os.makedirs(os.path.join(d, "tmp"))
    sh = SimpleNamespace(label=label, modules=sorted(modules, key=ctx.order.get),
                         started=time.monotonic(), result=os.path.join(d, "result.json"),
                         log=os.path.join(d, "log.txt"))
    left = max(1.0, ctx.deadline - time.monotonic()) + GRACE
    sh.cmd = [sys.executable, SHARD, "run", ctx.start, sh.result, f"{left:.0f}", *sh.modules]
    with open(sh.log, "w") as log:
        sh.proc = subprocess.Popen(sh.cmd, start_new_session=True, pass_fds=(ctx.fd,), stdout=log,
                                   stderr=subprocess.STDOUT,
                                   env=_child_env(TMPDIR=os.path.join(d, "tmp")))
    ctx.shards.append(sh)
    return sh


def _run_phase(label_of, groups, ctx):
    """Start one shard per group and wait for them all; False when --timeout ran out first."""
    pending = [_launch(label_of(i), g, ctx) for i, g in enumerate(groups, 1)]
    if pending:
        ctx.write_holder(ctx.holder_file, pending[0].proc)
    while pending:
        for sh in [s for s in pending if s.proc.poll() is not None]:
            pending.remove(sh)
            print(f"capsule-suite: shard {sh.label} ended with exit {sh.proc.returncode} after "
                  f"{time.monotonic() - sh.started:.0f}s ({len(sh.modules)} modules)", flush=True)
        if pending and time.monotonic() >= ctx.deadline:
            return False
        time.sleep(0.25)
    return True


def _stop_all(ctx, dump):
    live = [s.proc for s in ctx.shards if s.proc.poll() is None]
    for p in live if dump else ():
        with contextlib.suppress(OSError):    # gone already (macOS says EPERM for a zombie)
            os.kill(p.pid, signal.SIGUSR1)
    time.sleep(2 if dump and live else 0)
    for p in live:
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGKILL)
        p.wait()


def _show_log(sh):
    """The shard's output, and the command that replays its modules in the order it ran them."""
    with open(sh.log, errors="replace") as f:
        print(f"--- shard {sh.label} output ---\n{f.read()}", flush=True)
    print(f"capsule-suite: shard {sh.label} ran {len(sh.modules)} modules: {' '.join(sh.modules)}\n"
          f"capsule-suite: replay it with: {' '.join(sh.cmd[:4])} /dev/null 0 "
          f"{' '.join(sh.modules)}", flush=True)


def _covered(test_id, fixtures):
    """True when a skipped or errored setUpClass/setUpModule explains why test_id never ran."""
    return any(m and test_id.startswith(m.group(1) + ".") for m in map(FIXTURE.match, fixtures))


def _id_problems(modules, docs):
    """What differs between the ids discovery listed and the ids the shards ended."""
    want = collections.Counter(i for ids in modules.values() for i in ids)
    seen = collections.Counter(i for d in docs for i in d["ids"])
    fixtures = [f for d in docs for f in d["fixtures"]]
    missing = sorted(i for i in want - seen if not _covered(i, fixtures))
    extra = sorted(seen - want)
    out = []
    if missing:
        out.append(f"{len(missing)} listed tests never ran, e.g. {missing[:3]}")
    if extra:
        out.append(f"{len(extra)} tests ran that discovery did not list, e.g. {extra[:3]}")
    return out


def _shard_results(shards):
    """(summed counts, per-shard docs, seconds per module, failing ids, problems)."""
    tot, docs, secs, failed, problems = collections.Counter(), [], {}, [], []
    for sh in shards:
        doc, code = _load_json(sh.result), sh.proc.returncode
        clean = bool(doc) and not (doc["failures"] or doc["errors"] or doc["unexpected_successes"])
        if not doc or code != (0 if clean else 1):
            problems.append(f"shard {sh.label} ended with exit {code}"
                            + ("" if doc else " and no result"))
        if doc and doc["stopped"]:
            problems.append(f"shard {sh.label} was stopped early (result.stop())")
        if code != 0 or not doc:
            _show_log(sh)
        doc = doc or {"ids": [], "fixtures": [], "seconds": {}, "failed": [], "stopped": False}
        tot.update({k: v for k, v in doc.items() if isinstance(v, int) and not isinstance(v, bool)})
        docs.append(doc)
        secs.update(doc["seconds"])
        failed += doc["failed"]
    return tot, docs, secs, failed, problems


def _finish(ctx, listing, seconds_path, elapsed):
    """Sum the shards' results into unittest's last lines; 0 only if every check passed."""
    tot, docs, secs, failed, problems = _shard_results(ctx.shards)
    problems += _id_problems(listing["modules"], docs)
    for line in [f"FAILED test: {t}" for t in failed] + [f"PROBLEM: {p}" for p in problems]:
        print(f"capsule-suite: {line}", flush=True)
    if not problems:
        _save_seconds(seconds_path, secs, listing["modules"])
    return _report(tot, len(problems), elapsed)


def _save_seconds(path, secs, modules):
    with contextlib.suppress(OSError):
        with open(path + ".tmp", "w") as f:
            json.dump({m: round(s, 2) for m, s in secs.items() if m in modules}, f)
        os.replace(path + ".tmp", path)


def _report(tot, problems, elapsed):
    """unittest's own last lines, from the summed counts; exit 5 when nothing ran at all."""
    bad = [(n, v) for n, v in (("failures", tot["failures"]), ("errors", tot["errors"] + problems))
           if v]
    extra = [(n, tot[k]) for n, k in (("skipped", "skipped"),
                                      ("expected failures", "expected_failures"),
                                      ("unexpected successes", "unexpected_successes")) if tot[k]]
    failed = bool(bad or tot["unexpected_successes"])
    none = not failed and not tot["ran"] and not tot["skipped"]
    infos = ", ".join(f"{n}={v}" for n, v in bad + extra)
    n = tot["ran"]
    verdict = "FAILED" if failed else "NO TESTS RAN" if none else "OK"
    print(f"\nRan {n} test{'s' * (n != 1)} in {elapsed:.3f}s\n\n{verdict}"
          + (f" ({infos})" if infos else ""), flush=True)
    return 1 if failed else 5 if none else 0


def _discover(a, ctx):
    """(listing, None), or (None, exit code) when discovery failed, ran out of time or is empty."""
    out = os.path.join(ctx.root, "listing.json")
    try:
        subprocess.run([sys.executable, SHARD, "list", ctx.start, out], env=_child_env(),
                       timeout=a.timeout, check=True)
    except subprocess.TimeoutExpired:
        print(f"capsule-suite: TIMED OUT after {a.timeout:g}s during discovery", flush=True)
        return None, 124
    except subprocess.CalledProcessError:
        print("capsule-suite: discovery failed, nothing ran", flush=True)
        return None, 1
    listing = _load_json(out)
    if not any(listing["modules"].values()):
        return None, _report(collections.Counter(), 0, 0.0)
    return listing, None


def _timed_out(a, ctx):
    """Dump and kill what still runs, show the failed shards' logs; always exit 124."""
    print(f"capsule-suite: timed out after {a.timeout:g}s; SIGUSR1 for a faulthandler stack "
          "dump, then SIGKILL", flush=True)
    _stop_all(ctx, True)
    for sh in [s for s in ctx.shards if s.proc.returncode != 0]:
        with contextlib.suppress(OSError):    # an unreadable log must not change the exit
            _show_log(sh)
    print(f"capsule-suite: TIMED OUT after {a.timeout:g}s", flush=True)
    return 124


def _run_sharded(a, ctx, seconds_path):
    t0 = time.monotonic()
    listing, code = _discover(a, ctx)
    if listing is None:
        return code
    ctx.order = {m: i for i, m in enumerate(listing["modules"])}
    counts = {m: len(ids) for m, ids in listing["modules"].items()}
    serial = SERIAL_MAINTENANCE if a.maintenance else SERIAL
    groups, serial = _partition(counts, _seconds(seconds_path), serial, a.shards)
    print(f"capsule-suite: {sum(counts.values())} tests in {len(counts)} modules: "
          f"{len(groups)} parallel shards, {len(serial)} serial modules", flush=True)
    serial_groups = [serial] * bool(serial)
    if not (_run_phase(str, groups, ctx) and _run_phase(lambda i: "serial", serial_groups, ctx)):
        return _timed_out(a, ctx)
    return _finish(ctx, listing, seconds_path, time.monotonic() - t0)


def main(a, tests, fd, holder_file, write_holder):
    """Run the sharded suite under the lock the caller holds (fd); the shards inherit it."""
    ctx = SimpleNamespace(root=tempfile.mkdtemp(prefix="capsule-suite-shards-"), start=tests,
                          fd=fd, holder_file=holder_file, write_holder=write_holder, shards=[],
                          deadline=time.monotonic() + a.timeout, order={})
    name = "suite-maintenance" if a.maintenance else "suite"
    sigs = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    old = [signal.signal(sig, _terminated) for sig in sigs]
    try:
        return _run_sharded(a, ctx, os.path.join(a.lock_dir, f"{name}-shards.json"))
    except _Killed as k:
        _stop_all(ctx, False)
        print(f"capsule-suite: stopped by signal {k.signum}", flush=True)
        return 128 + k.signum
    finally:
        for sig, handler in zip(sigs, old):
            signal.signal(sig, handler)
        shutil.rmtree(ctx.root, ignore_errors=True)
