"""obs_probe_ship.py - the COPY half of obs_probe: to_csv() and ship() (split out of
obs_probe.py, card t_53c4b297; no behaviour change). obs_probe re-exports every name here,
so `obs_probe.ship` / `obs_probe.to_csv` keep working for callers and tests."""
import csv
import io
import json
import re
import sys
from pathlib import Path
from collections import namedtuple

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from capsule import paths  # noqa: E402
import events  # docker_run() - the same door db_migrate.py and events.py use


ShipResult = namedtuple("ShipResult", "ok skip_reason")

# The COPY's door being shut is not a probe bug: docker is not installed or its
# daemon is down, the container is absent, Postgres is not accepting
# connections, or the `samples` migration has not been applied yet. All of
# those are "come back next tick", and a 10-minute launchd job that exits 1 on
# them just manufactures job-failed pages for a known-down stack.
# Any OTHER non-zero psql exit is a real error and still exits 1.
_DOOR_SHUT_RE = re.compile(
    r"cannot connect to the docker daemon"
    r"|is the docker daemon running"
    r"|error during connect"
    r"|no such container"
    r"|container .* is not running"
    r"|connection refused"
    r"|could not connect to server"
    r"|the database system is starting up"
    r'|relation "?samples"?\s+does not exist',
    re.I)


def _door_shut(text):
    """The reason string when `text` names a shut door, else None."""
    text = (text or "").strip()
    if not text:
        return None
    return text[:200] if _DOOR_SHUT_RE.search(text) else None


def ship(csv_text, run):
    # "docker", not events.docker_bin(): docker_bin() is the *directory*
    # holding the engine's CLI (events.py:182), meant to be put on PATH via
    # docker_env() - every other psql door in this codebase (events._psql_copy,
    # db_migrate._default_psql) resolves the binary the same way. Literally
    # exec'ing docker_bin() itself raises EACCES (verified against the real
    # ops postgres container while implementing this).
    argv = ["exec", "-i", paths.PG_CONTAINER, "psql", "-U", "ops", "-d", "ops",
            "-v", "ON_ERROR_STOP=1", "-c",
            "COPY samples (ts, service, name, value, labels) FROM STDIN WITH (FORMAT csv)"]
    try:
        r = events.docker_run(argv, run, input=csv_text, text=True, capture_output=True, timeout=30)
    except FileNotFoundError as exc:
        # No `docker` on PATH at all - the shuttest door there is.
        print(f"obs_probe: ship: {exc}", file=sys.stderr)
        return ShipResult(False, f"docker not available ({exc})")
    except Exception as exc:
        print(f"obs_probe: ship: {exc}", file=sys.stderr)
        return ShipResult(False, _door_shut(str(exc)))
    if r.returncode == 0:
        # D255: best-effort. The raw rows are shipped and the next run recomputes the same
        # buckets, so a rollup failure only delays the weekly panels; it never fails the ship.
        try:
            u = events.docker_run(argv[:-2] + ["-c", ROLLUP_SQL], run, text=True,
                                  capture_output=True, timeout=30)
            if u.returncode != 0:
                print(f"obs_probe: rollup: {(u.stderr or 'psql failed').strip()[:200]}",
                      file=sys.stderr)
        except Exception as exc:
            print(f"obs_probe: rollup: {exc}", file=sys.stderr)
        return ShipResult(True, None)
    err = (r.stderr or "psql failed").strip()[:200]
    print(f"obs_probe: ship: {err}", file=sys.stderr)
    return ShipResult(False, _door_shut(err))


# D255: refresh the hourly rollup for the last 2 hours (the current bucket and the one before, so
# a run just after the hour still closes the previous bucket). One statement = one transaction.
ROLLUP_SQL = (
    "INSERT INTO samples_hourly (bucket, service, name, avg_value, min_value, max_value, n, "
    "total_avg, ticks) "
    "SELECT date_trunc('hour', ts), service, name, sum(s) / sum(c), min(mn), max(mx), sum(c), "
    "avg(s), count(*) FROM (SELECT ts, service, name, sum(value) s, count(*) c, min(value) mn, "
    "max(value) mx FROM samples WHERE ts >= date_trunc('hour', now()) - interval '1 hour' "
    "GROUP BY 1, 2, 3) t GROUP BY 1, 2, 3 "
    "ON CONFLICT (service, name, bucket) DO UPDATE SET avg_value = excluded.avg_value, "
    "min_value = excluded.min_value, max_value = excluded.max_value, n = excluded.n, "
    "total_avg = excluded.total_avg, ticks = excluded.ticks")
