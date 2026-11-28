#!/usr/bin/env python3
"""live_snapshot.py - one-minute snapshot of the live machine state the System Now dashboard shows
(W347). Board state is already mirrored into Postgres by the Lookout; what is NOT there is
containers, tick processes, disk, the offline test and the seat counts. Each run reads them and
replaces the rows of live_containers / live_health / live_seats in ONE transaction, so a reader
never sees half a snapshot.

Stdlib only. Postgres is reached the way db_migrate.py and lookout_smoke.py do it: `docker exec
psql` on capsule-corp-postgres through events.docker_run, script on stdin - no credential is read,
printed or put on argv. Read-only on everything it observes (seats via capsule.seats.counts, which
unlike snapshot() never opens incidents or sends notices)."""
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
import events  # noqa: E402
from capsule import host, paths  # noqa: E402

CONTAINER = paths.PG_CONTAINER
DISK_RED_GB = 25
SERVICES = (("postgres", paths.PG_CONTAINER, "Postgres"),
            ("loki", "capsule-corp-loki", "Loki"),
            ("grafana", "capsule-corp-grafana", "Grafana"),
            ("infisical", "capsule-corp-infisical", "Infisical"))
_STATUS_RE = re.compile(
    r"([\d.]+) h of (\d+) elapsed; (\d+) results, (\d+) PASS, (\d+) INFO, (\d+) FAIL")


# ------------------------------------------------------------------ containers
def _labels(raw):
    pairs = (part.partition("=") for part in (raw or "").split(","))
    return {key.strip(): val.strip() for key, sep, val in pairs if sep}


def _created(raw):
    """`2026-10-24 10:11:12 +0700 +07` -> ISO text, or None when it does not parse."""
    try:
        return datetime.datetime.strptime((raw or "")[:25], "%Y-%m-%d %H:%M:%S %z").isoformat()
    except ValueError:
        return None


def _json_objects(text):
    """The objects of `docker ps --format json`: one per line, or one array. Junk is skipped."""
    text = (text or "").strip()
    if text.startswith("["):
        return json.loads(text)
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _container_row(o):
    labels = _labels(o.get("Labels"))
    return {"name": (o.get("Names") or "").split(",")[0],
            "kind": "worker" if labels.get("capsule-run-id") else "service",
            "image": o.get("Image") or "", "status": o.get("Status") or "",
            "state": o.get("State") or "", "up_for": o.get("RunningFor") or "",
            "created_at": _created(o.get("CreatedAt")),
            "board": labels.get("capsule-board"), "run_id": labels.get("capsule-run-id")}


def _parse_containers(text):
    rows = [_container_row(o) for o in _json_objects(text) if isinstance(o, dict)]
    return [r for r in rows if r["name"]]


# ---------------------------------------------------------------------- health
def _tick_boards(pgrep_text, default_board):
    """Boards with a live `capsule tick`, from `pgrep -fl` lines; a tick without --board
    serves the default board."""
    lines = [ln for ln in (pgrep_text or "").splitlines()
             if "capsule tick" in ln and "pgrep" not in ln]
    found = [re.search(r"--board[ =](\S+)", ln) for ln in lines]
    return {m.group(1) if m else default_board for m in found}


def _parse_dark_status(text, rc):
    """(ok, detail) for the offline test: no start marker is not a failure, FAIL results are."""
    m = _STATUS_RE.search(text or "")
    if m:
        fails = int(m.group(6))
        return fails == 0, f"{m.group(1)} h of {m.group(2)}; {m.group(4)} pass, {fails} fail"
    if rc == 2 or "no start marker" in (text or ""):
        return True, "no offline test running"
    return False, "offline test status unreadable"


def _build_health(containers, tick_set, boards, disk_free_gb, dark):
    """[(check_name, label, ok, value, detail)] - one row per tile of the health strip."""
    rows = [(f"loop:{b}", f"{b} factory loop", b in tick_set, None,
             "tick process found" if b in tick_set else "no tick process") for b in boards]
    disk_ok = disk_free_gb is not None and disk_free_gb >= DISK_RED_GB
    disk_note = f"{disk_free_gb:.0f} GB free" if disk_free_gb is not None else "unknown"
    rows.append(("disk", "Disk free", disk_ok, disk_free_gb, disk_note))
    state = {c["name"]: c["state"] for c in containers}
    for key, name, label in SERVICES:
        rows.append((f"svc:{key}", label, state.get(name) == "running", None,
                     state.get(name) or "not found"))
    rows.append(("offline_test", "Offline test", dark[0], None, dark[1]))
    return rows


# ----------------------------------------------------------------------- seats
def _build_seats(counts, caps):
    """[(seat, used, cap, holder)] from capsule.seats.counts() and the config caps. An external
    seat used by an outside chat (`caps.external_heavy`, when the config has it) is taken off
    the building cap."""
    ext = int(caps.get("external_heavy", 0) or 0)
    return [("building", counts["heavy"], max(int(caps["max_heavy"]) - ext, 0), None),
            ("review", counts["review"], int(caps["max_review"]), None),
            ("keeper", counts["landers"], 1, "a merge keeper" if counts["landers"] else None)]


def _seat_counts(home):
    """(counts, cfg, limits) from the first board under home; raises when none is readable."""
    from capsule import config, db, paths, seats
    found = (d for d in (Path(home) / "boards").iterdir() if (d / "kanban.db").is_file())
    boards = sorted(d.name for d in found)
    cfg = config.load(paths.resolve_config(home, boards[0]))
    conn = db.connect_ro_tolerant(paths.board_db(boards[0], home))
    try:
        return seats.counts(conn, home, boards[0], time.time()), cfg, seats.limits(cfg)
    finally:
        conn.close()


def _read_seats(home):
    """Best effort: [] when no board or config can be read (the dashboard then shows no seats)."""
    try:
        counts, cfg, (review_cap, heavy_cap) = _seat_counts(home)
    except Exception as exc:
        print(f"live_snapshot: seats unreadable: {exc}", file=sys.stderr)
        return []
    caps = {"max_heavy": heavy_cap, "max_review": review_cap,
            "external_heavy": cfg["caps"].get("external_heavy", 0)}
    return _build_seats(counts, caps)


# ------------------------------------------------------------------------- sql
def _lit(v):
    """A SQL literal: NULL, number, boolean or a quoted string (NUL stripped)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\0", "").replace("'", "''") + "'"


def _insert(table, cols, rows):
    if not rows:
        return ""
    vals = ",\n".join("(" + ", ".join(_lit(v) for v in r) + ", now())" for r in rows)
    return f"insert into {table} ({', '.join(cols)}, snap_ts) values\n{vals};\n"


def _build_sql(containers, health, seats):
    """The whole snapshot as one transaction: delete every row, insert the fresh ones."""
    cols = ("name", "kind", "image", "status", "up_for", "created_at", "board", "run_id")
    crow = [tuple(c[k] for k in cols) for c in containers]
    return ("begin;\ndelete from live_containers where true;\n"
            "delete from live_health where true;\ndelete from live_seats where true;\n"
            + _insert("live_containers", cols, crow)
            + _insert("live_health", ("check_name", "label", "ok", "value", "detail"), health)
            + _insert("live_seats", ("seat", "used", "cap", "holder"), seats) + "commit;\n")


def _docker(args, **kw):
    """`docker *args` through the one guarded door; None (and a stderr line) when docker is
    missing, hung or refused - never raises."""
    try:
        return events.docker_run(args, capture_output=True, text=True, **kw)
    except Exception as exc:
        print(f"live_snapshot: docker {args[0]} failed: {type(exc).__name__}", file=sys.stderr)
        return None


def _write(sql):
    """True when Postgres took the snapshot. A failure is a stderr line, not an error: the
    Snapshot age tile on the dashboard shows the staleness."""
    if os.environ.get("AGENT_OPS_NO_SEND"):      # unit tests: never reach Postgres
        return True
    r = _docker(["exec", "-i", CONTAINER, "psql", "-U", "ops", "-d", "ops", "-q",
                 "-v", "ON_ERROR_STOP=1", "-f", "-"], input=sql, timeout=60)
    if r is not None and r.returncode != 0:
        print(f"live_snapshot: psql: {(r.stderr or '').strip()[:200]}", file=sys.stderr)
    return r is not None and r.returncode == 0


# ------------------------------------------------------------------- observing
def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def _dark_result():
    """`dark_test status` run in this process (no second Python): its printed summary and exit
    code go through the same parser, so exit 2 (no start marker) stays green."""
    out = io.StringIO()
    try:
        import dark_test
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = dark_test._cmd_status(None)
    except Exception as exc:
        return False, f"offline test status unreadable: {type(exc).__name__}"
    return _parse_dark_status(out.getvalue(), rc)


def _disk_free_gb():
    try:
        return shutil.disk_usage(str(Path.home())).free / 1e9
    except OSError:
        return None


def _bare_tick_board(home):
    """The board a bare `capsule tick` serves: the legacy `home/config.json` `board` field (what the
    tick itself reads), else host.json `boards.default`."""
    try:
        legacy = json.loads(paths.config_path(Path(home)).read_text()).get("board")
    except (OSError, ValueError, AttributeError):
        legacy = None
    return legacy or host.get_or_default("boards.default")


def _observe(home):
    """Read the machine: (containers, health rows, seat rows), or None when docker gave no
    answer (nothing to snapshot, and Postgres lives in docker). Never raises."""
    ps = _docker(["ps", "--format", "json"], timeout=30)
    if ps is None or ps.returncode != 0:
        print("live_snapshot: docker ps gave no answer; snapshot skipped", file=sys.stderr)
        return None
    containers = _parse_containers(ps.stdout)
    pg = _run(["pgrep", "-fl", "capsule tick"])
    boards_dir = Path(home) / "boards"
    boards = sorted(d.name for d in boards_dir.iterdir()) if boards_dir.is_dir() else []
    ticking = _tick_boards(pg.stdout if pg else "", _bare_tick_board(home))
    health = _build_health(containers, ticking, boards, _disk_free_gb(), _dark_result())
    return containers, health, _read_seats(home)


def _trim_log(path, max_bytes=262144, keep_lines=200):
    """The launchd plist appends stdout/stderr to one rolling log; cut it back to its last
    lines in place when it passes max_bytes (launchd keeps its append descriptor)."""
    try:
        big = path.stat().st_size > max_bytes
        tail = path.read_text(errors="replace").splitlines()[-keep_lines:] if big else []
    except OSError:
        return
    if big:
        with open(path, "r+", encoding="utf-8") as f:
            f.truncate(0)
            f.write("\n".join(tail) + "\n")


def main():
    """Always 0: a skipped or failed snapshot is a stderr line, and the dashboard's Snapshot
    age tile turns red. Silent on success."""
    from capsule import paths
    home = paths.home()
    _trim_log(home / "logs" / "live-snapshot.log")
    seen = _observe(home)
    if seen is not None and not _write(_build_sql(*seen)):
        print("live_snapshot: snapshot not written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
