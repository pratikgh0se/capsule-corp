#!/usr/bin/env python3
"""events.py - the one place every script and agent records what happened."""
import argparse, csv, datetime, io, json, os, re, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path

import docker_engine
import scrub


def state_root():
    """The one maintenance state dir. AGENT_OPS_STATE_DIR wins;
    under AGENT_OPS_NO_SEND (tests) a temp dir, never the real root; else
    ~/.local/state/capsule-corp/maint, the same from every checkout or copy."""
    override = os.environ.get("AGENT_OPS_STATE_DIR")
    if override:
        return Path(override)
    if os.environ.get("AGENT_OPS_NO_SEND"):
        return Path(tempfile.gettempdir()) / "capsule-corp-state-no-send"
    try:
        home = Path.home()
    except RuntimeError:  # no HOME and no passwd entry (worker container uid)
        home = Path(tempfile.gettempdir())
    return home / ".local" / "state" / "capsule-corp" / "maint"


def _default_spool():
    """EVENTS_SPOOL wins. Otherwise: under AGENT_OPS_NO_SEND (tests) a temp file, never
    `state/events.jsonl` - a test module that forgets the override would otherwise
    append fake rows that events.ship copies into Postgres and the daily report reads
    (the deployed maintenance dir is a symlink to the main checkout, so running the
    suite there pollutes live observability)."""
    override = os.environ.get("EVENTS_SPOOL")
    if override:
        return Path(override)
    if os.environ.get("AGENT_OPS_NO_SEND"):
        # CAPSULE_NO_SEND_SPOOL gives a test run its own file; the shared one is the fallback.
        own = os.environ.get("CAPSULE_NO_SEND_SPOOL")
        if own:
            return Path(own)
        return Path(tempfile.gettempdir()) / "capsule-corp-events-no-send.jsonl"
    return state_root() / "events.jsonl"


SPOOL = _default_spool()
COPY_COLS = "v,event_id,ts,kind,source,board,task_id,slug,title,text,link,severity,payload"
EVENT_V = 1

# kind -> the payload keys a consumer may rely on.
KINDS = {
    "card":           ("event",),
    "job":            ("job",),
    "health":         ("tick",),
    "audit":          (),
    "message":        (),
    "usage":          (),
    "vault":          (),
    "ai_usage":       (),      # ai_usage.py:195, read by the budget + home dashboards
    "backup_done":    (),      # backup.sh:119
    "backup_partial": (),      # backup.sh:119
    "ledger_mismatch": ("class", "board"),   # (retired W341: no writer left)
    "infra":          ("component",),   # heal.sh, one per check per tick
    # capsule/tick.py Tick._heartbeat, one per Capsule tick pass -
    "tick_health":    ("pass", "pid"),
                               # the dead-man signal alert `capsule-tick-deadman`
                               # watches (Grafana observability audit, 2026-09-27).
    "tick_stale_restart": ("loaded",),   # capsule/__main__.py _stale_exit: tick code moved
    "host_gap":       ("gap_s",),   # capsule/__main__.py _log_gap: the Mac slept or froze
    "release":        ("repo", "version"),   # Row 138. The
                               # ONLY emitter is maintenance/release_gate.py
                               # (test_events.test_release_has_exactly_one_emitter).
    "dep_cache":      ("event", "repo"),   # row 217
    "capsule_unblocked":  (),   # row 470 (capsule/metrics.py's tick step): one row per tick pass,
                               # a snapshot of the `capsule unblocked --json` detector's
                               # cached doc (capsule/unblocked, a sibling branch - stub
                               # interface here).
}


class EventInvalid(ValueError):
    """A row that does not match KINDS. Raised only under _strict()."""


def _strict():
    """Strict under the test marker AGENT_OPS_NO_SEND (already set by every test
    module) and under an explicit AGENT_OPS_EVENTS_STRICT=1."""
    return bool(os.environ.get("AGENT_OPS_EVENTS_STRICT")
                or os.environ.get("AGENT_OPS_NO_SEND"))


def _validate(kind, payload):
    """The complaint about (kind, payload), or "" when it is fine."""
    if kind not in KINDS:
        return f"unknown kind {kind!r} (known: {', '.join(sorted(KINDS))})"
    missing = [k for k in KINDS[kind] if not (payload or {}).get(k)]
    if missing:
        return f"kind {kind!r} needs payload {', '.join(missing)}"
    return ""

# A worker's stdout can carry NULs and other control bytes.
_CTRL = {c: None for c in list(range(0, 9)) + [0x0b, 0x0c] + list(range(0x0e, 0x20))}


def _clean(v):
    return v.translate(_CTRL) if isinstance(v, str) else v


def _scrub_payload(v):
    """Recursively scrub every str leaf of a payload value (dict/list/str),
    leaving non-str leaves alone."""
    if isinstance(v, str):
        return scrub.scrub(v)
    if isinstance(v, dict):
        return {k: _scrub_payload(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub_payload(x) for x in v]
    return v


def log(kind, source, text, *, board=None, task_id=None, slug=None, title=None,
        link=None, severity="info", payload=None, spool=None):
    """`v` and `event_id` lead the row: a consumer that does not recognise the version
    skips it, and `event_id` is what lets a re-shipped spool row be deduped in
    Postgres."""
    text, title, link = _clean(text), _clean(title), _clean(link)
    text, title, link = scrub.scrub(text), scrub.scrub(title), scrub.scrub(link)
    payload = payload if isinstance(payload, dict) else {}
    payload = _scrub_payload(payload)
    why = _validate(kind, payload)
    if why:
        if _strict():
            raise EventInvalid(f"events: invalid ({why})")
        # Never drop the row in production: log()'s contract is that it does not
        # break the caller, and the row is the one signal saying something is wrong.
        print(f"events: invalid ({why})", file=sys.stderr)
    row = {"v": EVENT_V, "event_id": uuid.uuid4().hex,
           "ts": time.time(), "kind": kind, "source": source, "board": board, "task_id": task_id,
           "slug": slug, "title": title, "text": text, "link": link, "severity": severity,
           "payload": payload}
    try:
        p = Path(spool or SPOOL)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # the sync tick must never die because of the trail
        print(f"events: not written ({exc})", file=sys.stderr)
    return row


def read(spool=None, since_s=None):
    p = Path(spool or SPOOL)
    if not p.exists():
        return []
    cutoff = (time.time() - since_s) if since_s else None
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if cutoff is None or float(r.get("ts", 0)) >= cutoff:
            out.append(r)
    return out


def needs_me(rows):
    """Open decisions: state=open rows whose key (task_id or slug) has no LATER state=closed
    row. Rows are assumed to be in file/time order; the last state seen per key wins, so a
    row can reopen a previously-closed key (state is not a permanent flag on the key)."""
    last = {}
    for r in rows:
        key = r.get("task_id") or r.get("slug")
        state = (r.get("payload") or {}).get("state")
        if not key or not state:
            continue
        last[key] = r if state == "open" else None
    now = time.time()
    return [dict(r, age_s=int(now - float(r["ts"]))) for r in last.values() if r]


def summary(rows):
    counts, merged, failed = {}, [], []
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        p = r.get("payload") or {}
        if p.get("event") == "merged":
            merged.append(r.get("title") or r.get("task_id") or "?")
        if r["kind"] == "job" and p.get("exit") not in (None, 0):
            failed.append(p.get("job") or "?")
    return {"counts": counts, "merged": merged, "failed": failed}


def _stage_script(csv_text):
    """COPY into a temp table in the same session, then INSERT ..."""
    # Belt-and-braces against the failure mode: a row terminator psql would reject
    # can never reach the script, whoever produced the CSV.
    body = csv_text.replace("\r\n", "\n")
    body = body if body.endswith("\n") else body + "\n"
    return ("BEGIN;\n"
            "CREATE TEMP TABLE events_stage (LIKE events INCLUDING DEFAULTS) ON COMMIT DROP;\n"
            f"COPY events_stage({COPY_COLS}) FROM STDIN WITH (FORMAT csv, FORCE_NULL (event_id));\n"
            + body + "\\.\n"
            f"INSERT INTO events ({COPY_COLS}) SELECT {COPY_COLS} FROM events_stage "
            "ON CONFLICT (event_id) WHERE event_id IS NOT NULL DO NOTHING;\n"
            "COMMIT;\n")


def docker_engine_row():
    """The selected engine (docker_engine.py): CAPSULE_DOCKER_ENGINE, then
    <state root>/docker-engine."""
    return docker_engine.resolve(state_root())


def __getattr__(name):
    """`events.DOCKER_BIN` (kept for callers) is the selected engine's CLI dir, read at access
    time."""
    if name == "DOCKER_BIN":
        return docker_engine_row()["bin"]
    raise AttributeError(f"module 'events' has no attribute {name!r}")


def docker_bin():
    """The engine's CLI dir (not on a launchd job's PATH). Read at call time, not import time:
    deploy.sh and the shell tests set the override in the environment of the process they launch."""
    return os.environ.get("AGENT_OPS_DOCKER_BIN") or docker_engine_row()["bin"]


def docker_env(env=None, guard=True, docker="docker"):
    """`os.environ` with the docker CLI dir in front of PATH and DOCKER_CONTEXT pinned to the
    engine's (installing another engine rewrites the shared ~/.docker/config.json currentContext).
    Under unittest a docker that resolves to the live install raises, NO_SEND or not (cards
    rq_6dd2a60d, rq_5b644c43); live means any engine's CLI, compared by realpath (OrbStack links
    /usr/local/bin/docker). An absolute `docker` is judged by its own path, a bare one by
    AGENT_OPS_DOCKER_BIN."""
    base = os.environ if env is None else env
    eng = docker_engine_row()
    fake = docker if os.path.isabs(docker) else shutil.which(
        "docker", path=base.get("AGENT_OPS_DOCKER_BIN") or os.devnull)
    skip = os.path.realpath(base.get("AGENT_OPS_DOCKER_BIN") or os.devnull)
    every = [os.path.expanduser(e["bin"]) for e in docker_engine.ENGINES.values()]
    search = (*base.get("PATH", "").split(":"), *os.defpath.split(":"))
    # a fake dir is not the live one
    dirs = [eng["bin"], *every, *(d for d in search if os.path.realpath(d) != skip)]
    live = {os.path.realpath(p) for p in (shutil.which("docker", path=d) for d in dirs) if p}
    if guard and "unittest" in sys.modules and (not fake or os.path.realpath(fake) in live):
        raise RuntimeError("a test process resolved the live ops-pg: set "
                           "AGENT_OPS_DOCKER_BIN to a fake docker dir (rq_6dd2a60d, rq_5b644c43)")
    return {**base, "PATH": docker_bin() + ":" + base.get("PATH", ""),
            "DOCKER_CONTEXT": eng["context"]}


_DOCKER_CMD = re.compile(r"(?:^|[;&|(]\s*)(?:\S*/)?docker(?:\s|$)")


def _names_docker(args):
    if isinstance(args, (str, bytes)):
        return bool(_DOCKER_CMD.search(os.fsdecode(args)))
    return bool(args) and os.path.basename(os.fsdecode(args[0])) == "docker"


def _refusing_popen(real):
    def popen(args, *a, **kw):
        if _names_docker(args):
            raise RuntimeError("an injected run tried to exec docker (the live ops-pg): inject a "
                               "fake run, or use the default run with AGENT_OPS_DOCKER_BIN")
        return real(args, *a, **kw)
    return popen


def docker_run(args, run=None, docker="docker", **kw):
    """The one way to run docker: `docker *args` with docker_env()."""
    env = docker_env(kw.pop("env", None), guard=run is None, docker=docker)
    if run is None or "unittest" not in sys.modules:
        return (run or subprocess.run)([docker, *args], env=env, **kw)
    from unittest import mock   # reached only when unittest is already loaded
    with mock.patch("subprocess.Popen", _refusing_popen(subprocess.Popen)):
        return run([docker, *args], env=env, **kw)


_OUTAGE = {"failed": 0}   # consecutive failed psql copies in this process


def _ship_outage(msg):
    """Log a psql failure once when an outage starts, count the repeats silently."""
    _OUTAGE["failed"] += 1
    if _OUTAGE["failed"] == 1:
        print(f"events.ship: {msg} (outage started; further failures are counted, not logged)",
              file=sys.stderr)
    return False


def _psql_copy(csv_text):
    if os.environ.get("AGENT_OPS_NO_SEND"):  # unit tests: never reach Postgres either
        return True
    try:
        r = docker_run(["exec", "-i", "capsule-corp-postgres", "psql", "-U", "ops",
                        "-d", "ops", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                       input=_stage_script(csv_text), text=True, capture_output=True, timeout=30)
        if r.returncode != 0:
            return _ship_outage(f"psql: {r.stderr.strip()[:200]}")
        if _OUTAGE["failed"]:
            print(f"events.ship: psql reachable again after {_OUTAGE['failed']} failed attempts",
                  file=sys.stderr)
            _OUTAGE["failed"] = 0
        return True
    except Exception as exc:
        return _ship_outage(str(exc))


def ship(spool=None, offset_path=None, copier=None):
    try:
        spool = Path(spool or SPOOL)
        offset_path = Path(offset_path) if offset_path else spool.with_suffix(".offset")
        if not spool.exists():
            return 0
        try:
            start = int(offset_path.read_text())
        except Exception:
            start = 0
        data = spool.read_bytes()
        if start > len(data):  # spool was truncated/rotated
            start = 0
        chunk = data[start:]
        if not chunk.strip():
            return 0
        # `lineterminator="\n"`, NOT the csv module's default CRLF: this CSV is embedded
        # as COPY data inside the script `_stage_script` builds and `psql -f -` reads,
        # and psql's script-mode copy handler rejects a CRLF data line ("unquoted
        # newline found in data").
        buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n"); n = 0
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
                ts = datetime.datetime.fromtimestamp(float(r["ts"]), datetime.timezone.utc).isoformat()
                w.writerow([int(r.get("v") or EVENT_V), r.get("event_id") or "",
                            ts, r["kind"], r["source"], r.get("board") or "", r.get("task_id") or "",
                            r.get("slug") or "",
                            _clean(r.get("title") or ""), _clean(r.get("text") or ""),
                            _clean(r.get("link") or ""), r.get("severity") or "info",
                            json.dumps(r.get("payload") or {})])
            except Exception:
                continue
            n += 1
        if n and (copier or _psql_copy)(buf.getvalue()):
            offset_path.write_text(str(len(data)))
            return n
        return 0
    except Exception as exc:
        print(f"events.ship: {exc}", file=sys.stderr)
        return 0


def _since(s):
    n, unit = int(s[:-1]), s[-1]
    return n * {"h": 3600, "d": 86400, "m": 60}[unit]


def main(argv=None):
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("log"); a.add_argument("kind"); a.add_argument("source"); a.add_argument("text")
    for k in ("board", "task", "slug", "title", "link", "severity", "state"):
        a.add_argument(f"--{k}")
    a.add_argument("--job"); a.add_argument("--phase", choices=("start", "end"))
    a.add_argument("--exit", type=int); a.add_argument("--duration", type=int)
    a.add_argument("--skipped"); a.add_argument("--target"); a.add_argument("--payload")
    a.add_argument("--session", help="claude session id (resume pointer): claude -r <id>")
    t = sub.add_parser("tail"); t.add_argument("--since", default="24h"); t.add_argument("--needs-me", action="store_true"); t.add_argument("--summary", action="store_true")
    sub.add_parser("ship")
    args = ap.parse_args(argv)
    if args.cmd == "ship":
        print(f"shipped {ship()}")
        return 0
    if args.cmd == "log":
        payload = {}
        if args.state:
            payload["state"] = args.state
        if args.job is not None:
            payload["job"] = args.job
        if args.phase is not None:
            payload["phase"] = args.phase
        if args.exit is not None:
            payload["exit"] = args.exit
        if args.duration is not None:
            payload["duration_s"] = args.duration
        if args.skipped is not None:
            payload["skipped"] = args.skipped
        if args.target is not None:
            payload["target"] = args.target
        if args.session is not None:
            payload["session"] = args.session
        if args.payload:
            payload.update(json.loads(args.payload))
        log(args.kind, args.source, args.text, board=args.board, task_id=args.task, slug=args.slug,
            title=args.title, link=args.link, severity=args.severity or "info",
            payload=payload or None)
        return 0
    rows = read(since_s=_since(args.since))
    if args.needs_me:
        for r in needs_me(rows):
            print(f"{r['age_s']//60:>5} min  {r.get('title') or r['text'][:60]}  {r.get('link') or ''}")
    elif args.summary:
        print(json.dumps(summary(rows), indent=1))
    else:
        for r in rows:
            print(time.strftime("%H:%M", time.localtime(r["ts"])), r["kind"], r["source"], (r.get("title") or "")[:40], "|", r["text"][:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
