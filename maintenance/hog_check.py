#!/usr/bin/env python3
"""Frozen-process check: list (preview, the default) or stop (--apply) runaway helper processes.

Frozen = at or above 90% CPU on two samples at least 60 s apart, running at least 2 hours, and
of a known kind (by program path from `ps -Ao pid,ppid,pcpu,etime,comm`): browser helper or
renderer, matched by app bundle (Google Chrome, Arc, Brave Browser, Microsoft Edge, Firefox);
orphaned Python run (a python whose parent is gone, ppid 1, and whose arguments contain
unittest or capsule-suite; a `-m capsule` python is never one); orphaned Claude CLI daemon
(bare `claude`, ppid 1, a --bg* helper argument, no controlling terminal). Arguments and tty
are read per process only to decide that: never returned, printed, logged or stored; the
environment is never read.
Never listed: system processes (/System, /usr/libexec, /usr/sbin, WindowServer, kernel_task),
an app's main executable, every pid `launchctl list` shows (its children stay eligible; if launchctl
cannot answer the run refuses), the tick, this process and its parent, anything started less
than 2 hours ago.
--apply: SIGTERM, wait up to 10 s, SIGKILL for any still alive; before each signal the pid is
re-read (same program, same kind, still 2 h old). One line per process (pid, kind, CPU, age,
result). No candidates: says so, exits 0.
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import time

CPU_MIN, MIN_AGE_S, GRACE_S, INTERVAL_S = 90.0, 2 * 3600, 10, 60
SYSTEM_PREFIXES = ("/System/", "/usr/libexec/", "/usr/sbin/")
SYSTEM_NAMES = ("WindowServer", "kernel_task")
BROWSER_APPS = ("Google Chrome", "Arc", "Brave Browser", "Microsoft Edge", "Firefox")
HELPER_WORDS = ("helper", "renderer", "plugin-container", "web content")
APP_PATH = re.compile(r"(?:^|/)Applications/([^/]+)\.app/")
MAIN_EXE = re.compile(r"^/(?:Users/[^/]+/)?Applications/[^/]+\.app/Contents/MacOS/[^/]+$")


class Refusal(Exception):
    pass


def _etime_s(text):
    m = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", text.strip())
    if not m:
        raise ValueError(text)
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def ps_rows():
    """{pid: (ppid, cpu, age_s, comm)} from one `ps` call (comm only, never args or env)."""
    try:
        r = subprocess.run(["ps", "-Ao", "pid,ppid,pcpu,etime,comm"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise Refusal(f"ps could not run: {e}")
    if r.returncode != 0:
        raise Refusal(f"ps failed rc={r.returncode}")
    rows = {}
    for line in r.stdout.splitlines()[1:]:
        f = line.split(None, 4)
        try:
            rows[int(f[0])] = (int(f[1]), float(f[2]), _etime_s(f[3]), f[4])
        except (ValueError, IndexError):
            continue
    return rows


def launchd_pids():
    """Every pid `launchctl list` (user domain) shows: pid, status, label per row. If it cannot
    answer (cannot run, non-zero, no pid at all) the run is refused: an empty protected set
    would be a guess."""
    try:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise Refusal(f"launchctl list could not run: {e}")
    rows = (line.split() for line in r.stdout.splitlines())
    pids = {int(f[0]) for f in rows if len(f) == 3 and f[0].isdigit()}
    if r.returncode != 0 or not pids:
        raise Refusal(f"launchctl list gave no usable answer (rc={r.returncode}); nothing stopped")
    return pids


def proc_flags(pid):
    """Yes/no facts about one process from `ps -o tty=,args=`: "test" (args name unittest or
    capsule-suite), "bg" (a --bg* helper argument), "tty" (a controlling terminal). The text
    is read to decide that only: never returned, printed, logged or stored."""
    try:
        r = subprocess.run(["ps", "-o", "tty=,args=", "-p", str(pid)], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {"tty"}                     # cannot tell: treated as a live, protected process
    tty, _, args = r.stdout.strip().partition(" ")
    facts = {"test"} if "unittest" in args or "capsule-suite" in args else set()
    facts |= {"bg"} if any(w.startswith("--bg") for w in args.split()) else set()
    return facts | ({"tty"} if tty and not set(tty) <= {"?"} else set())


def kind_of(ppid, comm, pid, flags):
    """The known kind of a process, or None (also None for anything protected by name/path)."""
    base = comm.rsplit("/", 1)[-1]
    if comm.startswith(SYSTEM_PREFIXES) or base in SYSTEM_NAMES or "capsule-tick" in comm.lower():
        return None
    if MAIN_EXE.match(comm):               # an app's own main executable, never a helper
        return None
    app = APP_PATH.search(comm)
    if app and app.group(1) in BROWSER_APPS:
        return "browser helper" if any(w in base.lower() for w in HELPER_WORDS) else None
    if ppid != 1 or app:
        return None
    # a `python -m capsule ...` (tick, lookout) has no unittest/capsule-suite in its args: never
    if base.lower().startswith("python") and "test" in flags(pid):
        return "orphaned python run"
    if base == "claude":
        f = flags(pid)
        return "idle claude helper" if "bg" in f and "tty" not in f else None
    return None


def candidates(first, second, protected, flags=proc_flags):
    """[(pid, kind, cpu, age_s, comm)] frozen on both samples."""
    out = []
    for pid, (ppid, cpu, age, comm) in second.items():
        before = first.get(pid)
        kind = kind_of(ppid, comm, pid, flags)
        if not kind or before is None or before[3] != comm:
            continue
        if min(cpu, before[1]) < CPU_MIN or age < MIN_AGE_S or pid in (os.getpid(),):
            continue
        if pid in protected:
            continue
        out.append((pid, kind, cpu, age, comm))
    return sorted(out)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _send(kill, pid, sig):
    """None when sent, else why not."""
    try:
        kill(pid, sig)
    except OSError as e:
        return f"failed: {e.strerror or e}"


def _still(pid, kind, comm, rows, launchd, flags):
    """The pid re-read: same program, same kind, still 2 h old, not a launchd job."""
    now = rows().get(pid)
    return (now is not None and now[3] == comm and now[2] >= MIN_AGE_S
            and kind_of(now[0], now[3], pid, flags) == kind and pid not in launchd())


def stop(pid, kind, comm, kill=os.kill, alive=_alive, sleep=time.sleep, rows=ps_rows,
         launchd=launchd_pids, flags=proc_flags):
    """SIGTERM, wait up to GRACE_S, SIGKILL if still alive. Returns the result word. Before each
    signal the pid is re-read (_still); if it changed, nothing is sent."""
    if not _still(pid, kind, comm, rows, launchd, flags):
        return "changed before stop; skipped"
    failed = _send(kill, pid, signal.SIGTERM)
    if failed:
        return failed
    for _ in range(GRACE_S):
        if not alive(pid):
            return "terminated"
        sleep(1)
    if not alive(pid):
        return "terminated"
    if not _still(pid, kind, comm, rows, launchd, flags):
        return "changed before kill; skipped"
    return _send(kill, pid, signal.SIGKILL) or "killed"


def run(apply, interval=INTERVAL_S, sleep=time.sleep, rows=ps_rows, launchd=launchd_pids,
        kill=os.kill, alive=_alive, out=print, flags=proc_flags):
    first = rows()
    sleep(interval)
    second = rows()
    protected = launchd() | {os.getpid(), os.getppid()}
    found = candidates(first, second, protected, flags)
    if not found:
        out("hog-check: no frozen processes")
        return 0
    for pid, kind, cpu, age, comm in found:
        result = (stop(pid, kind, comm, kill, alive, sleep, rows, launchd, flags) if apply
                  else "would stop (preview; --apply stops)")
        out(f"pid {pid} {kind} {comm.rsplit('/', 1)[-1]} cpu {cpu:.0f}% "
            f"age {age // 3600}h{age % 3600 // 60:02d}m: {result}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true", help="stop them; the default is a preview")
    p.add_argument("--interval", type=float, default=INTERVAL_S, help="seconds between samples")
    a = p.parse_args(argv)
    try:
        return run(a.apply, max(a.interval, INTERVAL_S))
    except Refusal as e:
        print(f"hog-check: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
