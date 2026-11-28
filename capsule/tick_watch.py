"""Tick watchdog (stall supervisor, unit 4a): is each board's tick alive, or wedged?

`python3 -m capsule tick-watch [--apply] [--json]`. `decide` is a pure function. The beat is
`<board dir>/tick.beat` (`{"at": epoch, "pid": int}`), written by the tick each pass
(`capsule/__main__.py`); no beat is no signal. Unit 4b: stop marker, 6 h crash restarts, rollback.
With no `--apply` nothing runs and nothing is paged or saved.
`--apply` restarts a wedged or dead tick once per call, on the 2nd bad sample (240 s after the
1st), and pages (Mac notification + one `outbox.log` line, repeat each 30 min).
The state per board is `<board dir>/tick-watch.json`.
"""
import contextlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from capsule import config, db, incidents, outbox, paths, tick_guard

WEDGE_S = 600              # a beat older than this on a live tick is a wedge
CRASH_N, CRASH_WINDOW_S = 2, 3600     # a 3rd restart inside the window is a crash loop
REPEAT_S = 1800            # a page repeats at 30 min, never earlier
SETTLE_S = 240             # a bad tick is restarted on the 2nd sample (heal runs each 300 s)
RESTART_TIMEOUT_S = 240
CRASH_RESTART_S = 6 * 3600     # in a crash loop: one restart per 6 h
GOOD_S = 3600              # a commit with this much of good beats is `last_good_sha`
ROLLBACK_WINDOW_S = 86400  # roll back only if the checkout moved in the last 24 h, once a day
STOP_FILE = "tick-stop.json"     # `<board dir>/tick-stop.json`: `{"by": str, "until": epoch}`
PARK_FILES = ("park.json", "hoi-poi.json")    # `capsule park` and `hoi-poi down` stamps
SCHEMA_FILES = ("capsule/board_schema.sql", "capsule/db.py")    # the schema version = these files
PING_TIMEOUT_S = 10
HOST_GAP_S = 120
SHARED_KEYS = ("last_good_sha", "rolled_back_at")
ROLLBACK_RETRY_S = 3600    # after a failed rollback, wait this long before the next try
SHARED_FILE = "tick-rollback.json"    # `<home>/...`: last_good_sha, rolled_back_at (one checkout)


def host_gap(prev_wall, prev_mono, wall, mono):
    """Seconds the wall clock gained on the process clock between two readings, or None when it
    is 120 s or less. A sleeping Mac stops the process clock and not the wall clock."""
    gap = int((wall - prev_wall) - (mono - prev_mono))
    return gap if gap > HOST_GAP_S else None


def _track_sha(state, sha, now):
    """Remember when the release checkout last moved; the first sha seen is not a move."""
    if sha and state.get("sha_seen") != sha:
        state["sha_changed_at"] = now if state.get("sha_seen") else None
        state["sha_seen"] = sha


def _can_roll_back(state, sha, now, schema_same, is_ancestor):
    last = state.get("last_good_sha")
    moved = state.get("sha_changed_at")
    if not (last and sha and last != sha and moved is not None and now - moved < ROLLBACK_WINDOW_S):
        return False
    if now - (state.get("rolled_back_at") or -ROLLBACK_WINDOW_S) < ROLLBACK_WINDOW_S:
        return False
    if now - (state.get("rollback_failed_at") or -ROLLBACK_RETRY_S) < ROLLBACK_RETRY_S:
        return False
    if is_ancestor is not None and not is_ancestor(last, sha):
        return False       # a rollback never moves forward
    return bool(schema_same and schema_same(last, sha))


def _good_beat(state, restarts, sha, now):
    """An ok sample: track the good run; 1 h of good beats sets `last_good_sha` and ends a loop."""
    good = state.get("good_since")
    if not good or sha != state.get("good_sha"):
        good = now        # the good run starts again when the checkout moves
    if sha and now - good >= GOOD_S:
        state["last_good_sha"] = sha
    state.update(restarts=restarts, resumed=bool(state.get("bad_since")), bad_since=None,
                 paged_at=None, good_since=good, good_sha=sha)
    if now - good >= GOOD_S:
        state["crashloop_at"] = None      # only 1 h of good beats ends a crash loop


def _crash_action(state, sha, now, schema_same, is_ancestor):
    """In a crash loop: `rollback`, or one `restart` per 6 h, else `page`."""
    state["crashloop_at"] = state.get("crashloop_at") or now
    base = max(state.get("crash_restart_at") or 0, state["crashloop_at"])
    if _can_roll_back(state, sha, now, schema_same, is_ancestor):
        state["rolled_back_at"] = now
        return "rollback"
    return "restart" if now - base >= CRASH_RESTART_S else "page"


def decide(beat_age_s, loaded, dispatcher, state, now, stop_marker=None, sha=None,
           schema_same=None, is_ancestor=None):
    """`(verdict, action, new_state)`. `beat_age_s` None = no signal. `dispatcher` is
    `tick_guard.dispatcher`'s answer. Verdicts: ok, wedged, dead, stopped, crashloop. Actions:
    none, restart, start, rollback, page (a page is `none` while the last one is under 30 min old).
    `stop_marker` (`absent`, `ended`, `valid`; None = unknown): a stopped tick is started unless
    the marker is valid. A crash loop restarts once per 6 h; one `rollback` goes to `last_good_sha`
    (a commit with 1 h of good beats) when `sha` moved in the last 24 h, `schema_same(a_sha, b_sha)
    -> bool` is true and `is_ancestor(last, sha) -> bool` (None = not asked) is true. A valid stop
    marker means no start, no restart and no page, whatever the verdict. A crash loop lasts until
    1 h of good beats. The caller restores `rolled_back_at`, `crash_restart_at` and
    `restarts` after a failed rollback only; a failed restart keeps its stamp and pages."""
    state = dict(state or {})
    restarts = [t for t in state.get("restarts") or [] if now - t < CRASH_WINDOW_S]
    _track_sha(state, sha, now)
    # no beat (None) is never a wedge; after a restart the new tick gets WEDGE_S to beat
    grace = bool(restarts) and now - restarts[-1] < WEDGE_S
    wedged = beat_age_s is not None and beat_age_s > WEDGE_S and not grace
    if not loaded:
        verdict = "stopped"
    elif dispatcher == "none":
        verdict = "dead"
    elif wedged:
        verdict = "wedged"
    else:
        _good_beat(state, restarts, sha, now)
        return "ok", "none", state
    state.update(restarts=restarts, resumed=False, good_since=None)
    state["bad_since"] = state.get("bad_since") or now
    if stop_marker == "valid":
        return verdict, "none", state       # a deliberate stop: no start, restart or page
    action = "restart"
    if verdict == "stopped":
        action = {"valid": "none", "absent": "start", "ended": "start"}.get(stop_marker, "page")
    elif len(restarts) >= CRASH_N or state.get("crashloop_at"):
        verdict, action = "crashloop", _crash_action(state, sha, now, schema_same, is_ancestor)
    if action == "page":
        if now - (state.get("paged_at") or -REPEAT_S) < REPEAT_S:
            action = "none"
        else:
            state["paged_at"] = now
    elif action in ("restart", "rollback") and verdict == "crashloop":
        state["restarts"], state["crash_restart_at"] = restarts + [now], now
    elif action == "restart" and now - state["bad_since"] < SETTLE_S:
        action = "none"       # the first sample: a deploy may have just started the tick
    elif action == "restart":
        state["restarts"] = restarts + [now]
    return verdict, action, state


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _agents():
    return Path(os.environ.get("CAPSULE_LA_DIR") or Path.home() / "Library" / "LaunchAgents")


def _plist(board):
    found = sorted(_agents().glob(f"*.capsule-tick-{board}.plist"))
    return found[0] if found else None


def _boards(home):
    """A board is any `boards/<name>/` with a kanban.db or a config.json, or any installed
    `*.capsule-tick-<name>.plist` whose `boards/<name>/` exists."""
    names = {p.parent.name for pat in ("kanban.db", "config.json")
             for p in (home / "boards").glob(f"*/{pat}")}
    names |= {n for n in (p.name[:-len(".plist")].split(".capsule-tick-", 1)[1]
                          for p in _agents().glob("*.capsule-tick-*.plist"))
              if (home / "boards" / n).is_dir()}
    return sorted(names)


def stop_marker(bdir, now, state_dir=None):
    """`valid` (a deliberate stop is on), `ended` (its time is over) or `absent`. A park or
    hoi-poi stamp in `state_dir` counts as a valid stop."""
    if state_dir and any((Path(state_dir) / name).is_file() for name in PARK_FILES):
        return "valid"
    until = _read_json(Path(bdir) / STOP_FILE).get("until")
    if not isinstance(until, (int, float)):
        return "absent"
    return "valid" if now < until else "ended"


def _git_runner(checkout):
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return r.returncode, (r.stdout or "").strip()
    return git


def _head(git):
    rc, out = git("rev-parse", "HEAD")
    return out if rc == 0 and out else None


def _is_ancestor(git, last, sha):
    return git("merge-base", "--is-ancestor", last, sha)[0] == 0


def _schema_same(git, last, sha):
    """The board schema files are the same at both commits."""
    for name in SCHEMA_FILES:
        (ra, oa), (rb, ob) = git("rev-parse", f"{last}:{name}"), git("rev-parse", f"{sha}:{name}")
        if ra or rb or oa != ob:
            return False
    return True


def _lander_row(dbp):
    """The `lander.checking` row of one board db (None when unset); raises `sqlite3.Error`."""
    conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT value FROM guard_state WHERE key = 'lander.checking'").fetchone()
    finally:
        conn.close()


def _lander_live(home):
    """A detached lander worker (`lander.checking` in a board's guard_state) is alive."""
    for board in _boards(home):
        dbp = Path(paths.board_db(board, home))
        if not dbp.exists():
            continue
        try:
            row = _lander_row(dbp)
        except sqlite3.Error:
            return True       # an unreadable board db: assume a lander is alive (fail closed)
        try:
            os.kill(int(json.loads(row[0])["pid"]), 0)
            return True
        except (OSError, TypeError, ValueError, KeyError):
            continue
    return False


def _rollback(git, sha, home):
    """Detach the release checkout at `sha`. False, with nothing moved, unless `git status` runs,
    the tree is clean and no lander is alive."""
    rc, dirty = git("status", "--porcelain")
    if rc != 0 or dirty or _lander_live(home):
        return False
    return git("checkout", "--detach", sha)[0] == 0


def _launchctl(*args):
    """True or False for launchctl's exit code; None when it cannot run (unknown, not lost)."""
    cmd = [os.environ.get("CAPSULE_LAUNCHCTL", "launchctl"), *args]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def _jobs_check(labels, home, now, parked, state_dir=None):
    """One `{"label", "state"}` per label. Not loaded and not held by a stop marker (a tick's
    marker, or the park stamp): `launchctl bootstrap` it. States: loaded, held, bootstrapped,
    bootstrap-failed. AGENT_OPS_NO_LAUNCHCTL=1: launchctl never runs, every job counts as loaded."""
    out, domain = [], f"gui/{os.getuid()}"
    for label in labels:
        plist = _agents() / f"{label}.plist"
        board = label.split(".capsule-tick-", 1)[1] if ".capsule-tick-" in label else None
        held = parked or (board and stop_marker(
            paths.board_dir(board, home), now, state_dir) == "valid")
        if os.environ.get("AGENT_OPS_NO_LAUNCHCTL") == "1" or _launchctl(
                "print", f"{domain}/{label}") is not False:
            state = "loaded"
        elif held:
            state = "held"
        else:
            state = "bootstrapped" if plist.is_file() and _launchctl(
                "bootstrap", domain, str(plist)) else "bootstrap-failed"
        out.append({"label": label, "state": state})
    return out


def _deadman_ping(home, get=None):
    """One GET to the first `unattended.deadman_url`: no data in the request, nothing when none is
    set or under AGENT_OPS_NO_SEND=1. Never raises. True when sent and answered."""
    urls = [config.unattended(cfg).get("deadman_url") for cfg in (
        config.Loader(paths.config_path(home, b)).get()[0] for b in _boards(home)) if cfg]
    url = next((u for u in urls if u), None)
    if not url or os.environ.get("AGENT_OPS_NO_SEND"):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    try:
        # http(s) only, checked above
        req = urllib.request.Request(url, headers={"User-Agent": "capsule-deadman"})  # noqa: S310
        opener = get or urllib.request.urlopen  # noqa: S310
        with opener(req, timeout=PING_TIMEOUT_S) as resp:
            return 200 <= int(resp.status) < 400
    except Exception:
        return False


def _loaded(plist):
    """launchd has the job. Under AGENT_OPS_NO_LAUNCHCTL=1 launchctl is never run: an installed
    plist counts as loaded."""
    if os.environ.get("AGENT_OPS_NO_LAUNCHCTL") == "1":
        return True
    label = plist.name[:-len(".plist")]
    try:
        r = subprocess.run([os.environ.get("CAPSULE_LAUNCHCTL", "launchctl"), "print",
                            f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return True       # unknown is not stopped
    return r.returncode == 0


def _restart(board, home, verb="restart"):
    """Run the restart (or `start`) command once; True when it exits 0. The default restarts only
    this board's tick and leaves the release checkout where it is (`--no-update`)."""
    custom = os.environ.get("CAPSULE_TICK_WATCH_CMD")
    cmd = (shlex.split(custom) + [board] if custom else
           [str(paths.REPO / "bin" / "capsule-tick.sh"), verb, board, "--no-update",
            "--wait", "120"])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=RESTART_TIMEOUT_S,
                           env={**os.environ, "CAPSULE_HOME": str(home)})
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _notify(board, home, text, now):
    log = paths.outbox_log(home, board)
    outbox.send_local({"thread_key": None, "body": text}, home=home, path=log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{int(now)}\ttick-watch: {text}\n")


def _page(board, home, verdict, now):
    """One Mac notification (or one `local-no-send.jsonl` line under AGENT_OPS_NO_SEND=1) and one
    `outbox.log` line. Never raises."""
    try:
        _notify(board, home, f"Tick {board}: {verdict}", now)
    except (OSError, subprocess.SubprocessError):
        return


def _save(path, state):
    try:
        path.write_text(json.dumps(state))
    except OSError:
        return


def _state_dir():
    """The maintenance state dir (maintenance/events.py state_root): where the park stamps are."""
    env = os.environ
    if env.get("STATE_DIR") or env.get("AGENT_OPS_STATE_DIR"):
        return env.get("STATE_DIR") or env["AGENT_OPS_STATE_DIR"]
    if env.get("AGENT_OPS_NO_SEND"):
        return str(Path(tempfile.gettempdir()) / "capsule-corp-state-no-send")
    return str(Path.home() / ".local" / "state" / "capsule-corp" / "maint")


def _unloaded(label):
    """The label is in `launchd/unloaded` (installed, never loaded): never start it."""
    pfx = label.split(".capsule-tick-", 1)[0]
    f = Path(os.environ.get("CAPSULE_LAUNCHD_UNLOADED") or paths.REPO / "launchd" / "unloaded")
    try:
        rows = [x.strip().replace("__PREFIX__", pfx) for x in f.read_text().splitlines()]
    except OSError:
        return False
    return label in rows


def _rolled_back(board, home, now, frm, to):
    """Page, and open a `silent_failure` incident, naming both shas."""
    text = f"crash loop, release checkout rolled back {frm[:12]} -> {to[:12]}"
    _page(board, home, text, now)
    if not paths.board_db(board, home).exists():
        return
    kw = dict(board=board, kind="silent_failure", now=now, evidence=[f"rolled back {frm} to {to}"],
              key=f"tick crash loop: checkout rolled back to {to[:12]}")
    try:
        with contextlib.closing(db.connect(paths.board_db(board, home))) as conn:
            incidents.open_incident(conn, **kw)
    except Exception as exc:
        print(f"tick-watch: incident not opened: {type(exc).__name__}: {exc}", file=sys.stderr)


def _rollback_failed(verdict, state, before, out, now):
    """A rollback that moved nothing: record it; a crash loop gets no stamp for the try."""
    out["action"] = "rollback-failed"
    state["rollback_failed_at"] = now
    if verdict == "crashloop":
        for key in ("rolled_back_at", "crash_restart_at", "restarts"):
            state[key] = before.get(key)
        state["restarts"] = state.get("restarts") or []


def _apply_action(action, verdict, state, before, out, where):
    """The side effects of one verdict; `where` is `(board, home, now, sha, git)`."""
    board, home, now, sha, git = where
    if action == "rollback":
        ok = _rollback(git, state["last_good_sha"], home)
        if not ok:
            _rollback_failed(verdict, state, before, out, now)
        else:
            _rolled_back(board, home, now, sha, state["last_good_sha"])
            if not _restart(board, home):    # the tick must run the old code now
                out["action"] = "rollback-restart-failed"
                state["paged_at"] = now
                _page(board, home, verdict, now)
    elif action in ("restart", "start"):
        ok = _restart(board, home, action)
        out["action"] = action if ok else action + "-failed"
        if not ok and verdict == "crashloop":
            # the try counts (crash_restart_at stays): the next pass pages, then waits 6 h
            state["paged_at"] = now
            _page(board, home, verdict, now)
    elif action == "page":
        _page(board, home, verdict, now)


def _check_board(board, home, now, apply, git=None):
    """One result object for one board (and, with `apply`, the side effects). `git(*args)` is the
    release-checkout seam, returning `(exit code, stdout)`; the default runs git in
    `<home>/harness/main`. `last_good_sha` and `rolled_back_at` live in `<home>/tick-rollback.json`:
    the checkout is one for every board."""
    bdir = paths.board_dir(board, home)
    plist = _plist(board)
    out = {"board": board, "verdict": "ok", "action": "none", "beat_age_s": None, "resumed": False}
    if plist is None:
        return out       # no tick installed here: nothing to watch
    beat = _read_json(bdir / "tick.beat").get("at")
    age = max(0, now - beat) if isinstance(beat, (int, float)) else None
    out["beat_age_s"] = None if age is None else round(age, 1)
    state_file, shared_file = bdir / "tick-watch.json", Path(home) / SHARED_FILE
    git = git or _git_runner(home / "harness" / "main")
    marker = stop_marker(bdir, now, _state_dir())
    if marker != "valid" and _unloaded(plist.stem):
        marker = None      # installed, never loaded: a page, never a start
    shared = _read_json(shared_file)
    before = {**_read_json(state_file), **{k: shared.get(k) for k in SHARED_KEYS}}
    sha = _head(git)
    verdict, action, state = decide(age, _loaded(plist), tick_guard.dispatcher(home, board),
                                    before, now, stop_marker=marker, sha=sha,
                                    schema_same=lambda a, b: _schema_same(git, a, b),
                                    is_ancestor=lambda a, b: _is_ancestor(git, a, b))
    out.update(verdict=verdict, action=action, resumed=bool(state.get("resumed")),
               marker=marker)
    if apply:
        _apply_action(action, verdict, state, before, out, (board, home, now, sha, git))
        _save(shared_file, {k: state.get(k) for k in SHARED_KEYS})
        _save(state_file, {k: v for k, v in state.items() if k not in SHARED_KEYS})
    return out


def add_parser(sub):
    p = sub.add_parser("tick-watch", help="is each board's tick alive? (--apply restarts a wedged"
                                          " or dead tick once and pages; default: read-only)")
    p.add_argument("--apply", action="store_true", help="run the restart and send the pages")
    p.add_argument("--json", action="store_true", help="one JSON object per board, one per line")
    p.add_argument("--jobs", action="store_true", help="heal check `jobs`: bootstrap each of"
                   " --labels that launchd lost (and no stop marker or --parked holds)")
    p.add_argument("--labels", nargs="*", default=[], help="launchd labels for --jobs")
    p.add_argument("--parked", action="store_true", help="--jobs: the pipeline is parked")
    p.add_argument("--ping", action="store_true", help="one GET to unattended.deadman_url, if set")
    p.add_argument("--capsule-home", default=None)


def handle(args, parser):
    home = paths.home(args.capsule_home)
    now = time.time()
    if args.ping:
        print(json.dumps({"sent": _deadman_ping(home)}))
        return 0
    if args.jobs:
        for res in _jobs_check(args.labels, home, now, args.parked, _state_dir()):
            print(json.dumps(res))
        return 0
    boards = _boards(home)
    if not boards and not args.json:
        print("tick-watch: no boards")
    for board in boards:
        res = _check_board(board, home, now, args.apply)
        if args.json:
            print(json.dumps(res))
            continue
        print(f"{board}: {res['verdict']} ({res['action']})")
        if res["resumed"]:
            print(f"{board}: resumed")
    return 0
