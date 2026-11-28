#!/usr/bin/env python3
"""dark_test.py - the measured 48-hour "no old home" test (plan-off-legacy, wave 3).

  dark_test.py start            refuse unless the old home is absent; write the start marker
                                (plus baselines: launchd run counts, log offsets, the outbox)
  dark_test.py list             every ai.*/<prefix>.* plist, included or excluded, with a reason
  dark_test.py check [--skip N] observe launchd's own runs and run the 3 read-only commands
  dark_test.py status           hours elapsed of 48, counts, the FAILs

The CoS moves the old home aside first; this runner never moves, renames or deletes it, and it never
launches a LaunchAgent job (launchd already does; a second copy would race it). `check` reads, per
included plist, launchd's last exit (`launchctl print`, read-only), the job's own log and
state/jobs/<job>.json, and outbox growth since `start`, and flags a job that never ran although
its schedule says it should have. It runs only `capsule check`, `capsule unblocked` and
`deploy.sh --check`. A verdict rests on exit status plus a log scan, so an old-home not-found
line FAILs even at exit 0.
"""
import argparse
import datetime
import json
import os
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import events  # noqa: E402
from capsule import legacy_names, paths, safe_write  # noqa: E402

REPO = Path(os.environ.get("DARK_TEST_REPO") or Path(__file__).resolve().parent.parent)
WINDOW_H = 48
TAIL_LINES = 20
GRACE_S = 900        # a scheduled run may start this late before it counts as "did not run"
# run.sh job -> (exit code, cause) for a refusal by design that needs a hand fix: INFO, no incident
NEEDS_HUMAN_EXIT = {"fork-sync": (3, "upstream merge conflict needs a hand merge"),
                    "retrieval-eval": (3, "recall floor regression needs a human look")}
SCRIPTS = ("run.sh", "report.sh", "capsule-keeper-watch.sh", "autosync.sh")
_GONE = r"no such file|not found|does not exist|cannot (open|find|access)|enoent|exit 127"
# "is_error" is no word (the "_" ends the letters); a quoted key name ("failed":0) is no error
_ERR = rf"\b(?:{_GONE}|[a-z]*errors?|errored|fail(?:s|ed|ing|ures?)?|denied|unable)\b(?!\"\s*:)"
_MENTION = legacy_names.MENTION_PATTERN
_NEAR = ".{0,80}"    # the mention and the error word must share one clause
LEGACY_MENTION = re.compile(_MENTION, re.I)
LEGACY_ERROR = re.compile(rf"({_MENTION}){_NEAR}({_ERR})|({_ERR}){_NEAR}({_MENTION})", re.I)
# (name, argv, (info exit or tuple of exits, what it means) or None): by design, not a crash
FIXED = (("capsule-check", ["python3", "-m", "capsule", "check"], None),
         ("capsule-unblocked", ["python3", "-m", "capsule", "unblocked"],
          (1, "work is waiting")),       # unblocked.py
         # deploy.sh --check: 1 drift, 4 observability-only drift
         ("deploy-check", ["./deploy.sh", "--check"], ((1, 4), "drift")))


def _dir():
    return events.state_root() / "dark-test"


def _write(path, text):
    safe_write.atomic_write(events.state_root(), path, text, create_dirs=True, mode=0o600)


# -- which plists, and what each one runs ---------------------------------------------------

def _launchd_prefix(agents):  # the one shared resolution is bin/lib_prefix.sh
    lib = Path(__file__).resolve().parent.parent / "bin" / "lib_prefix.sh"  # not REPO: a test repo
    cmd = ['. "$1"; capsule_launchd_prefix', "_", str(lib)]
    r = subprocess.run(["bash", "-c", *cmd], capture_output=True, text=True, timeout=15,
                       env={**os.environ, "CAPSULE_LA_DIR": str(agents)})
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "launchd prefix unresolved")
    return r.stdout.strip()


def _unloaded(prefix):
    """Install-only labels in launchd/unloaded, __PREFIX__ expanded (as bin/lib_jobs.sh)."""
    f = Path(os.environ.get("CAPSULE_LAUNCHD_UNLOADED")
             or Path(__file__).resolve().parent.parent / "launchd" / "unloaded")  # not REPO
    try:
        lines = [ln.strip() for ln in f.read_text().splitlines()]
    except OSError:
        return set()
    return {ln.replace("__PREFIX__", prefix) for ln in lines if ln and not ln.startswith("#")}


def _plists():
    agents = Path(os.environ.get("DARK_TEST_LAUNCHAGENTS") or Path.home() / "Library/LaunchAgents")
    found = []
    prefix = _launchd_prefix(agents)
    unloaded = _unloaded(prefix)
    for path in sorted(p for pat in ("ai.*.plist", f"{prefix}.*.plist") for p in agents.glob(pat)):
        data = plistlib.loads(path.read_bytes())
        label = data.get("Label") or path.stem
        found.append({"label": label, "data": data, "loaded_at": _mtime(path),
                      **_classify(data, path, unloaded)})
    return found


def _classify(data, path, unloaded=()):
    """{"included", "reason", "job", "script"}: why this plist is (not) a dark-test item."""
    label = data.get("Label") or path.stem
    if label in unloaded:
        return {"included": False, "reason": "install-only (launchd/unloaded)"}
    if label.startswith(legacy_names.LAUNCHD_LABEL_PREFIX):
        return {"included": False, "reason": "old gateway itself: the thing being retired"}
    if "capsule-tick" in label:
        return {"included": False, "reason": "tick daemon: covered by `capsule check`"}
    if "ollama-serve" in label:
        return {"included": False, "reason": "model server daemon: no old-system path"}
    tokens = [t for a in data.get("ProgramArguments") or [] for t in shlex.split(a)]
    for i, tok in enumerate(tokens):
        name = Path(tok).name
        if name not in SCRIPTS:
            continue
        job = None
        if Path(tok).parent.name == "maintenance":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            job = "report" if name == "report.sh" else nxt if nxt and nxt[0] != "-" else None
        return {"included": True, "reason": f"runs {name}" + (f" {job}" if job else ""),
                "job": job, "script": tok}
    return {"included": False, "reason": "runs none of run.sh, report.sh, keeper-watch, autosync"}


# -- reading launchd, logs, state ------------------------------------------------------------

def _launchd(label):
    """{"loaded", "runs", "exit"} from the read-only `launchctl print gui/<uid>/<label>`."""
    try:
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {"loaded": False, "runs": None, "exit": None}
    runs, last = (re.search(rf"{k} = (-?\d+)", r.stdout) for k in ("runs", "last exit code"))
    return {"loaded": r.returncode == 0, "runs": int(runs.group(1)) if runs else None,
            "exit": int(last.group(1)) if last else None}


NO_LOG = "plist sets no StandardOutPath/StandardErrorPath: launchd output is not captured"


def _std_paths(data):
    return [data[k] for k in ("StandardOutPath", "StandardErrorPath") if data.get(k)]


def _outbox():
    return sorted(p.name for p in (events.state_root() / "outbox").glob("*.msg"))


def _read_from(path, offset):
    try:
        with open(path, "rb") as fh:
            fh.seek(offset if os.fstat(fh.fileno()).st_size >= offset else 0)   # rotated: from 0
            return fh.read().decode(errors="replace").splitlines()
    except OSError:
        return []


_TOOL_KEYS = ("permission_denials", "tool_input")   # what the agent tried, never what failed


def _strings(v):
    """Every string value under v; key names and _TOOL_KEYS never count."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for k, x in v.items():
            if k not in _TOOL_KEYS:
                yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def _error_strings(v):
    """The error fields of an agent object, nested ones too: "result" counts only when the
    run reports an error, key names never."""
    if isinstance(v, list):
        for x in v:
            yield from _error_strings(x)
    elif isinstance(v, dict) and (v.get("is_error") or v.get("api_error_status") is not None):
        yield from _strings(v)
    elif isinstance(v, dict):
        for k, x in v.items():
            if k in _TOOL_KEYS:
                continue
            yield from _strings(x) if k in ("error", "errors") else _error_strings(x)


def _hit(line):
    try:
        obj = json.loads(line) if line.lstrip().startswith("{") else None
    except ValueError:
        obj = None   # starts with "{" but is shell text
    if obj is None:
        return bool(LEGACY_ERROR.search(line))
    return bool(LEGACY_MENTION.search("\n".join(_error_strings(obj))))


def _hits(lines):
    return [ln for ln in lines if _hit(ln)]


def _last_wake():
    """Epoch seconds of the host's last wake (`sysctl -n kern.waketime`), 0 on any error."""
    try:
        r = subprocess.run(["sysctl", "-n", "kern.waketime"], capture_output=True, text=True,
                           timeout=10)
        sec = re.search(r"sec = (\d+)", r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return int(sec.group(1)) if sec else 0


def _due(data, start, now):
    """Latest scheduled time in (start, now - grace], else None (unscheduled or not due yet)."""
    horizon = now - GRACE_S
    if data.get("StartInterval"):
        # launchd counts from when the job loaded, not from `start`: the newest run must be
        # younger than interval + grace, so only a job quiet since before that window is late
        latest = horizon - data["StartInterval"] - GRACE_S
        return latest if latest > start else None
    spec = data.get("StartCalendarInterval")
    specs = spec if isinstance(spec, list) else [spec] if spec else []
    latest = [_latest_match(s, start, horizon) for s in specs]
    return max((t for t in latest if t), default=None)


def _latest_match(spec, start, horizon):
    t = int(horizon) // 60 * 60
    while t > start:
        lt = time.localtime(t)
        if all(_match(k, v, lt) for k, v in spec.items()):
            return t
        t -= 60
    return None


def _match(key, want, lt):
    have = {"Minute": lt.tm_min, "Hour": lt.tm_hour, "Day": lt.tm_mday, "Month": lt.tm_mon,
            "Weekday": (lt.tm_wday + 1) % 7}[key]
    return have == want or (key == "Weekday" and want == 7 and have == 0)


# -- the verdict ----------------------------------------------------------------------------

def _judge(sig):
    """(status, reason, incident kind) from the signals; None signals are "not observed"."""
    if sig.get("timed_out"):
        return "FAIL", f"timed out after {sig['timeout']}s", "hang"
    if sig.get("loaded") is False:
        return "FAIL", "not loaded in launchd", "silent_failure"
    codes = {sig[k] for k in ("exit", "launchd_exit", "state_exit") if sig.get(k)}
    ie = sig.get("info_exit") or ()
    ie = {ie} if isinstance(ie, int) else set(ie)
    if codes and codes <= ie and len(codes) == 1 and not sig.get("hits"):
        return "INFO", f"exit {next(iter(codes))}: {sig.get('info_why') or 'work is waiting'}", None
    for key in ("exit", "launchd_exit", "state_exit"):
        if sig.get(key):
            named = f": FAIL {sig['fail_name']}" if key == "exit" and sig.get("fail_name") else ""
            return "FAIL", f"exit {sig[key]}{named}", "crash"
    if sig.get("hits"):
        return "FAIL", "legacy path error", "silent_failure"
    if sig.get("outbox_new"):
        return "FAIL", "send spooled to outbox", "silent_failure"
    if sig.get("did_not_run"):
        return "FAIL", "did not run", "silent_failure"
    return "PASS", None, None


def _row(name, sig, tail, t0):
    status, reason, kind = _judge(sig)
    zero = [k for k in ("exit", "launchd_exit", "state_exit") if sig.get(k) == 0]
    bad = bool(sig.get("hits") or sig.get("outbox_new") or sig.get("did_not_run"))
    return {"ts": int(time.time()), "item": name, "status": status, "reason": reason, "kind": kind,
            "exit": sig.get("exit", sig.get("launchd_exit")),
            "duration_s": round(time.time() - t0, 2),
            "signals": sig, "conflict": bool(zero and bad), "stderr_tail": tail[-TAIL_LINES:],
            "legacy_lines": (sig.get("hits") or [])[:5]}


def _run_fixed(name, argv, info, timeout):
    """One read-only command; a timeout kills its whole process group (no orphans)."""
    t0, sig, out = time.time(), {"timeout": timeout}, ""
    if info:
        sig["info_exit"], sig["info_why"] = info
    proc = subprocess.Popen(argv, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        sig["exit"] = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        sig["timed_out"] = True
    except OSError as exc:
        stdout, stderr, sig["exit"] = "", f"cannot run: {exc}", 127
    sig["hits"] = _hits((stdout + "\n" + stderr).splitlines())
    fail = re.search(r"^FAIL (\S+):", stdout, re.M)   # a check reports its failing row on stdout
    sig["fail_name"] = fail.group(1) if fail else None
    drift = re.search(r"^DRIFT.*$", stdout, re.M)       # deploy.sh --check names each drifted file
    if drift and info:
        sig["info_why"] = f"{info[1]}: {drift.group(0)}"
    return _row(name, sig, stdout.splitlines() + stderr.splitlines(), t0)


def _observe(plist, base, now):
    """Passive check of one launchd job since `start`: launchd exit, log scan, job state, outbox."""
    t0, start = time.time(), base["start_ts"]
    label, data, job = plist["label"], plist["data"], plist.get("job")
    ld = _launchd(label)
    runs = (ld["runs"] or 0) - base["launchd_runs"].get(label, 0)
    sig = {"loaded": ld["loaded"], "runs_since_start": runs,
           "launchd_exit": ld["exit"] if runs > 0 else None}
    if job in NEEDS_HUMAN_EXIT:
        sig["info_exit"], sig["info_why"] = NEEDS_HUMAN_EXIT[job]
    lines, newest = [], 0
    if not _std_paths(data):
        sig["log_na"] = NO_LOG   # a crash row from such a job has an empty tail by design
    for path in _std_paths(data):
        lines += _read_from(path, base["log_offsets"].get(path, 0))
        newest = max(newest, _mtime(path))
    if job:
        state = _job_state(job)
        end = _epoch(state.get("end")) if state else 0
        logs = [state["log"]] if state and end >= start else []
        logs += [str(p) for p in (Path(plist["script"]).parent / "logs").glob(f"{job}-*.log")
                 if p.stat().st_mtime >= start]
        for lg in dict.fromkeys(logs):
            lines += _read_from(lg, 0)
            newest = max(newest, _mtime(lg))
        sig["state_exit"] = state.get("exit") if state and end >= start else None
        sig["state_end"] = end
        sig["outbox_new"] = [n for n in _outbox()
                             if n not in base["outbox"] and n.endswith(f"-{job}.msg")]
    else:
        sig["outbox_na"] = "job does not write the maintenance outbox (only run.sh/report.sh do)"
    sig["hits"] = _hits(lines)
    if data.get("StartInterval"):   # a sleeping host runs nothing: count awake time only
        sig["last_wake"] = _last_wake()
    owed_from = start   # a job that joined after start is owed only slots after it joined
    if label not in base["launchd_runs"]:
        owed_from = max(start, base.get("joined", {}).get(label, 0))
    # A plist rewritten mid-window moves loaded_at forward, which can hide an earlier missed slot.
    # This bound overlaps the `joined` bound above: both skip slots before a start time.
    if data.get("StartCalendarInterval"):   # owed only the slots after the plist was installed
        owed_from = max(owed_from, plist.get("loaded_at", 0))
        sig["loaded_at"] = plist.get("loaded_at", 0)
    due = _due(data, max(owed_from, sig.get("last_wake", 0)), now)
    seen = max(sig.get("state_end", 0), newest)
    sig["due"] = due
    sig["did_not_run"] = bool(due) and seen < due and not (runs > 0 and not job)
    return _row(label, sig, lines, t0)


def _mtime(path):
    return int(os.path.getmtime(path)) if os.path.exists(path) else 0


def _epoch(iso):
    try:
        return int(datetime.datetime.fromisoformat(iso).timestamp())
    except (TypeError, ValueError):
        return 0


def _job_state(job):
    try:
        return json.loads((events.state_root() / "jobs" / f"{job}.json").read_text())
    except (OSError, ValueError):
        return None


def _filed():
    try:
        return json.loads((_dir() / "filed.json").read_text())
    except (OSError, ValueError):
        return {}


def _file_once(rec, results, filed):
    """Open an incident only for a fault (item + reason) not already filed this session:
    every check re-reads the logs from `start`, so a standing fault would refile each run."""
    key = f"{rec['item']}|{rec['reason']}"
    if key in filed:
        return f"already filed: {filed[key]}"
    out = _open_incident(rec, results)
    if not out.startswith("incident NOT opened"):
        filed[key] = out
        _write(_dir() / "filed.json", json.dumps(filed))
    return out


def _open_incident(rec, results):
    """Korin's rule: a FAIL opens an incident (the CLI front of incidents.open_incident).
    Never raises: a failed filing is recorded on the row and the run goes on."""
    argv = ["python3", "-m", "capsule", "incidents", "open", "--kind", rec["kind"],
            "--root-cause-key", f"dark test {rec['item']}: {rec['reason']}",
            "--evidence", str(results), "--board", "capsule-corp"]
    try:
        r = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, errors="replace",
                           timeout=120, env={**os.environ, "CAPSULE_INCIDENTS_BUSY_MS": "100000"})
    except Exception as exc:
        return f"incident NOT opened: {exc}"
    if r.returncode:
        return f"incident NOT opened: exit {r.returncode}: {r.stderr.strip()[-200:]}"
    return r.stdout.strip()[-300:] or "opened"


# -- commands -------------------------------------------------------------------------------

def _cmd_start(_args):
    old_home = paths.legacy_home()
    if os.path.lexists(old_home):
        print(f"dark_test: {old_home} exists; move it aside first (this runner never does)",
              file=sys.stderr)
        return 2
    marker = _dir() / "start.json"
    if marker.exists():
        print(f"dark_test: already started ({marker}); remove it to restart", file=sys.stderr)
        return 2
    inc = [p for p in _plists() if p["included"]]
    base = {"start_ts": int(time.time()), "legacy_home": str(old_home), "outbox": _outbox(),
            "launchd_runs": {p["label"]: _launchd(p["label"])["runs"] or 0 for p in inc},
            "log_offsets": {s: _size(s) for p in inc for s in _std_paths(p["data"])}}
    _write(marker, json.dumps(base))
    _write(_dir() / "filed.json", "{}")   # a restarted test must refile old faults
    _write(_dir() / "joined.json", "{}")
    print(f"dark test started; run `check` periodically for {WINDOW_H} h")
    return 0


def _size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def _cmd_list(_args):
    for p in _plists():
        note = " (no log path)" if p["included"] and not _std_paths(p["data"]) else ""
        print(f"{'included' if p['included'] else 'excluded'}  {p['label']}: {p['reason']}{note}")
    for name, argv, _info in FIXED:
        print(f"included  {name}: {' '.join(argv)} (run actively, read-only)")
    return 0


def _base():
    try:
        return json.loads((_dir() / "start.json").read_text())
    except (OSError, ValueError):
        return None


def _joined(base, now, plists):
    """First check time of each included label start.json does not list; written once."""
    try:
        joined = json.loads((_dir() / "joined.json").read_text())
    except (OSError, ValueError):
        joined = {}
    new = {p["label"]: int(now) for p in plists
           if p["included"] and p["label"] not in base["launchd_runs"]
           and p["label"] not in joined}
    if new:
        joined.update(new)
        _write(_dir() / "joined.json", json.dumps(joined))
    return joined


def _cmd_check(args):
    if os.path.lexists(paths.legacy_home()):
        print(f"dark_test: {paths.legacy_home()} exists; not a dark test", file=sys.stderr)
        return 2
    base = _base()
    if base is None:
        print("dark_test: no start marker; run `start` first", file=sys.stderr)
        return 2
    results = _dir() / "results.jsonl"
    timeout = int(os.environ.get("DARK_TEST_TIMEOUT", "1800"))
    now = time.time()
    plists = _plists()
    base["joined"] = _joined(base, now, plists)
    rows = [_run_fixed(n, a, i, timeout) for n, a, i in FIXED if n not in args.skip]
    rows += [_observe(p, base, now) for p in plists
             if p["included"] and p["label"] not in args.skip]
    fails, filed = 0, _filed()
    for rec in rows:
        if rec["status"] == "FAIL":
            fails += 1
            rec["incident"] = _file_once(rec, results, filed)
        with safe_write.open_append(events.state_root(), results) as fh:
            fh.write((json.dumps(rec, ensure_ascii=False) + "\n").encode())
        print(f"{rec['status']} {rec['item']} ({rec['reason'] or 'ok'})")
    return 1 if fails else 0


def _results(start):
    path = _dir() / "results.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return [r for r in map(json.loads, filter(str.strip, lines)) if r["ts"] >= start]


def _cmd_status(_args):
    base = _base()
    if base is None:
        print("dark_test: no start marker", file=sys.stderr)
        return 2
    start = base["start_ts"]
    rows = _results(start)
    fails = [r for r in rows if r["status"] == "FAIL"]
    infos = [r for r in rows if r["status"] == "INFO"]
    print(f"{(time.time() - start) / 3600:.1f} h of {WINDOW_H} elapsed; {len(rows)} results, "
          f"{len(rows) - len(fails) - len(infos)} PASS, {len(infos)} INFO, {len(fails)} FAIL")
    for r in fails:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
        print(f"FAIL {r['item']} ({r['reason']}) at {when}  incident: {r.get('incident')}")
    return 1 if fails else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("start", "list", "status"):
        sub.add_parser(name)
    sub.add_parser("check").add_argument("--skip", action="append", default=[],
                                         help="item name to leave out")
    args = p.parse_args(argv)
    run = {"start": _cmd_start, "list": _cmd_list, "check": _cmd_check, "status": _cmd_status}
    return run[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
