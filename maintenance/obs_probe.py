#!/usr/bin/env python3
"""obs_probe.py - Capsule's self-probe (docs/superpowers/specs/2026-09-09-
observability-build-1-design.md section 3, table `samples` from migration
002-samples.sql). One tick: collect ten signal groups (postgres, boards,
cards, docker, sandbox, launchd, bridge, heal, host, power), ship them as one COPY,
print one summary line. A collector that raises never stops the others - see collect().
Never a secret or a raw value in a log line; only the summary/skip lines.

Usage: python3 obs_probe.py     (called every 10 min by launchd, job obs-probe)
"""
import csv
import datetime
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_rounds  # SQL per board kind (row 583)
import probe_boards  # kanban_dbs(): $CAPSULE_HOME/boards (row 478 D8)
from obs_probe_util import _DIGIT_RE, _HEX_RE, _percentile, fingerprint  # noqa: F401
from obs_probe_ship import ShipResult, _door_shut, ship  # noqa: F401 (t_53c4b297)
_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from capsule import host, paths  # noqa: E402
import events  # docker_bin() / docker_env() - the same door db_migrate.py and events.py use
import obs_image_lock  # check(): worker image vs the IMAGES.lock capsule-python row (t_53c8a81a)

REPO = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO / "launchd" / "templates"
# The seven capsule-corp-* containers observability/docker-compose.yml defines.
CONTAINERS = (paths.PG_CONTAINER, "capsule-corp-loki", "capsule-corp-fluent-bit",
              "capsule-corp-grafana", "capsule-corp-infisical", "capsule-corp-infisical-db",
              "capsule-corp-infisical-redis")
SERVICE = "capsule"
COLLECTOR_NAMES = ("postgres", "boards", "cards", "docker", "sandbox", "launchd", "bridge", "heal", "host", "power")

Sample = namedtuple("Sample", "ts service name value labels")

# name -> {"collector": str, "labels": tuple[str, ...], "unit": str}. Single
# source of truth for every emitted sample name - Tasks 3 and 4 import this to
# build panels/alerts, and test_obs_probe asserts every emitted Sample.name is
# a key here and every key here is emitted by the fixtures, so this dict can
# never silently drift from what the probe actually ships.
NAMES = {
    "capsule.postgres.connections":     {"collector": "postgres", "labels": ("state",),          "unit": "1"},
    "capsule.postgres.db.bytes":        {"collector": "postgres", "labels": (),                   "unit": "By"},
    "capsule.postgres.table.rows":      {"collector": "postgres", "labels": ("table",),           "unit": "1"},
    "capsule.postgres.table.bytes":     {"collector": "postgres", "labels": ("table",),           "unit": "By"},
    "capsule.postgres.events.per_hour": {"collector": "postgres", "labels": (),                   "unit": "1"},
    "capsule.postgres.wal.bytes":       {"collector": "postgres", "labels": (),                   "unit": "By"},

    "capsule.board.cards":              {"collector": "boards", "labels": ("board", "status"),    "unit": "1"},
    "capsule.board.runs.24h":           {"collector": "boards", "labels": ("board", "outcome"),   "unit": "1"},
    "capsule.board.run.duration_s.p50": {"collector": "boards", "labels": ("board",),              "unit": "s"},
    "capsule.board.run.duration_s.p95": {"collector": "boards", "labels": ("board",),              "unit": "s"},
    "capsule.board.failures.open":      {"collector": "boards", "labels": ("board",),              "unit": "1"},
    "capsule.board.db.bytes":           {"collector": "boards", "labels": ("board",),              "unit": "By"},
    "capsule.board.wal.bytes":          {"collector": "boards", "labels": ("board",),              "unit": "By"},

    "capsule.card.runs":               {"collector": "cards", "labels": ("board", "task_id"),                  "unit": "1"},
    "capsule.card.duration_s":         {"collector": "cards", "labels": ("board", "task_id"),                  "unit": "s"},
    "capsule.card.review_rounds":      {"collector": "cards", "labels": ("board", "task_id"),                  "unit": "1"},
    "capsule.card.errors":             {"collector": "cards", "labels": ("board", "task_id"),                  "unit": "1"},
    "capsule.card.error_repeats":      {"collector": "cards", "labels": ("board", "task_id"),                  "unit": "1"},

    "capsule.container.cpu.pct":        {"collector": "docker", "labels": ("container",),         "unit": "%"},
    "capsule.container.mem.bytes":      {"collector": "docker", "labels": ("container",),         "unit": "By"},
    "capsule.container.restarts":       {"collector": "docker", "labels": ("container",),         "unit": "1"},
    "capsule.container.up":             {"collector": "docker", "labels": ("container",),         "unit": "1"},

    "capsule.sandbox.containers":        {"collector": "sandbox", "labels": (),                    "unit": "1"},
    "capsule.sandbox.network.attached":  {"collector": "sandbox", "labels": ("container",),        "unit": "1"},

    "capsule.job.loaded":               {"collector": "launchd", "labels": ("label",),            "unit": "1"},
    "capsule.job.last_exit":            {"collector": "launchd", "labels": ("job",),               "unit": "1"},
    "capsule.job.last_duration_s":      {"collector": "launchd", "labels": ("job",),               "unit": "s"},
    "capsule.job.age_s":                {"collector": "launchd", "labels": ("job",),               "unit": "s"},

    "capsule.bridge.tick.age_s":        {"collector": "bridge", "labels": (),                      "unit": "s"},
    "capsule.bridge.tick.exit":         {"collector": "bridge", "labels": (),                      "unit": "1"},
    "capsule.bridge.ledger.pushed":     {"collector": "bridge", "labels": (),                      "unit": "1"},

    "capsule.heal.fails":               {"collector": "heal", "labels": ("component",),            "unit": "1"},

    "capsule.host.disk.pct":            {"collector": "host", "labels": (),                        "unit": "%"},
    "capsule.host.load1":               {"collector": "host", "labels": (),                        "unit": "1"},

    "capsule.delegation.rulings.7d":   {"collector": "power", "labels": (),                     "unit": "1"},
    "capsule.delegation.decisions.7d": {"collector": "power", "labels": (),                     "unit": "1"},
    # Row 201 (A1). The size of the vault-chunk block a card carried: rendered
    # host-side at dispatch, so its estimate is replayed from the
    # `retrieval_push` event board_bridge logs (see POWER_SQL). A series of its
    # own; no digest line reads it any more.
    "capsule.card.retrieval.tokens":  {"collector": "power", "labels": ("board", "task_id"),   "unit": "1"},
}


def _now(now):
    return now or datetime.datetime.now(datetime.timezone.utc)


def _sample(now, name, value, **labels):
    if name not in NAMES:
        raise ValueError(f"obs_probe: {name!r} is not a NAMES key")
    return Sample(ts=now.isoformat(), service=SERVICE, name=name, value=float(value), labels=labels)


# ---------------------------------------------------------------- postgres --
# One psql call, -t -A --csv (tuples-only, unaligned, csv: no header, no
# framing). k/l/count columns; l is '' for the scalar rows.
POSTGRES_SQL = (
    "select 'connections' as k, state as l, count(*)::float from pg_stat_activity where datname='ops' group by state\n"
    "union all select 'db.bytes', '', pg_database_size('ops')\n"
    "union all select 'table.rows', relname, n_live_tup::float from pg_stat_user_tables where relname in ('events','samples')\n"
    "union all select 'table.bytes', relname, pg_total_relation_size(relid)::float from pg_stat_user_tables where relname in ('events','samples')\n"
    "union all select 'events.per_hour', '', count(*)::float from events where ts > now() - interval '1 hour'\n"
    "union all select 'wal.bytes', '', coalesce(sum(size),0)::float from pg_ls_waldir();\n"
)


def collect_postgres(run, now=None):
    now = _now(now)
    # "docker", not events.docker_bin(): see the comment on ship() below -
    # docker_bin() is the directory events.docker_env() puts on PATH, not an
    # executable. Exec'ing it directly raises EACCES every tick, silently
    # (caught by collect()'s per-collector try/except), dropping every
    # postgres sample forever - caught in review, fixed here the same way
    # ship() already was.
    argv = ["exec", "-i", paths.PG_CONTAINER, "psql", "-U", "ops", "-d", "ops",
            "-t", "-A", "--csv", "-v", "ON_ERROR_STOP=1", "-c", POSTGRES_SQL]
    r = events.docker_run(argv, run, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "psql failed").strip()[:200])
    out = []
    for k, l, v in csv.reader(io.StringIO(r.stdout or "")):
        v = float(v)
        if k == "connections":
            # `state` is NULL for a backend that has no state yet (psql's --csv
            # renders that as an empty field); an empty label value would ship a
            # `{"state": ""}` row that no panel legend can name. Call it what it
            # is instead.
            out.append(_sample(now, "capsule.postgres.connections", v, state=l or "unknown"))
        elif k == "db.bytes":
            out.append(_sample(now, "capsule.postgres.db.bytes", v))
        elif k == "table.rows":
            out.append(_sample(now, "capsule.postgres.table.rows", v, table=l))
        elif k == "table.bytes":
            out.append(_sample(now, "capsule.postgres.table.bytes", v, table=l))
        elif k == "events.per_hour":
            out.append(_sample(now, "capsule.postgres.events.per_hour", v))
        elif k == "wal.bytes":
            out.append(_sample(now, "capsule.postgres.wal.bytes", v))
    return out


# ------------------------------------------------------------------ boards --
def collect_boards(home, now=None):
    """One board per iteration, each wrapped: a board whose kanban.db is
    corrupt, locked, or missing a table (an old board without `task_runs`)
    prints `obs_probe: boards skipped <board>: <exc>` and contributes nothing,
    while every other board still emits (the collector-level
    try/except in collect() used to drop all boards for one bad one)."""
    now = _now(now)
    out = []
    cutoff = now.timestamp() - 86400
    for db_path in probe_boards.kanban_dbs(home):
        board = db_path.parent.name
        try:
            out.extend(_collect_board(db_path, board, cutoff, now))
        except Exception as exc:
            print(f"obs_probe: boards skipped {board}: {exc}", file=sys.stderr)
    return out


def _collect_board(db_path, board, cutoff, now):
    out = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        for status, count in cur.execute("select status, count(*) from tasks group by status"):
            out.append(_sample(now, "capsule.board.cards", count, board=board, status=status))
        outcomes = {}
        durations = []
        for outcome, started_at, ended_at in cur.execute(
                "select outcome, started_at, ended_at from task_runs where started_at > ?", (cutoff,)):
            key = outcome or "running"
            outcomes[key] = outcomes.get(key, 0) + 1
            if ended_at is not None:
                durations.append(ended_at - started_at)
        for outcome, count in outcomes.items():
            out.append(_sample(now, "capsule.board.runs.24h", count, board=board, outcome=outcome))
        durations.sort()
        p50 = _percentile(durations, 0.50)
        p95 = _percentile(durations, 0.95)
        if p50 is not None:
            out.append(_sample(now, "capsule.board.run.duration_s.p50", p50, board=board))
        if p95 is not None:
            out.append(_sample(now, "capsule.board.run.duration_s.p95", p95, board=board))
        (n_open,) = cur.execute("select count(*) from tasks where consecutive_failures > 0").fetchone()
        out.append(_sample(now, "capsule.board.failures.open", n_open, board=board))
    finally:
        con.close()
    out.append(_sample(now, "capsule.board.db.bytes", os.path.getsize(db_path), board=board))
    wal = db_path.with_name(db_path.name + "-wal")
    out.append(_sample(now, "capsule.board.wal.bytes", wal.stat().st_size if wal.exists() else 0, board=board))
    return out


# -------------------------------------------------------------------- cards --
# The meter (design 2026-09-09-power-level, section 7.1): per-card rollups the
# board db already knows.
CARD_RUN_CAP = 200      # label cardinality: the newest 200 runs per board


def collect_cards(home, now=None):
    """One board per iteration, each wrapped: exactly collect_boards' rule, so
    a corrupt board, a locked board or a board with no task_runs contributes
    nothing while every other board still emits."""
    now = _now(now)
    out = []
    for db_path in probe_boards.kanban_dbs(home):
        board = db_path.parent.name
        try:
            out.extend(_collect_cards(db_path, board, now))
        except Exception as exc:
            print(f"obs_probe: cards skipped {board}: {exc}", file=sys.stderr)
    return out


def _collect_cards(db_path, board, now):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        runs = list(cur.execute(
            "select task_id, started_at, ended_at, coalesce(error,'')"
            " from task_runs order by id desc limit ?", (CARD_RUN_CAP,)))
        marked = (Path(db_path).parent / review_rounds.MARKER).exists()   # row 583
        try:
            rounds = dict(cur.execute(review_rounds.SQL[marked]))
        except sqlite3.Error:
            rounds = {}   # a board predating task_events still emits the run-level samples
    finally:
        con.close()

    per_card = {}
    for task_id, started_at, ended_at, error in runs:
        c = per_card.setdefault(task_id, {"runs": 0, "dur": 0.0, "errors": 0, "fp": {}})
        c["runs"] += 1
        c["dur"] += float((ended_at if ended_at is not None else started_at) - started_at)
        if error:
            c["errors"] += 1
            fp = fingerprint(error)
            c["fp"][fp] = c["fp"].get(fp, 0) + 1

    out = []
    for task_id, c in sorted(per_card.items()):
        lb = {"board": board, "task_id": task_id}
        out.append(_sample(now, "capsule.card.runs", c["runs"], **lb))
        out.append(_sample(now, "capsule.card.duration_s", c["dur"], **lb))
        out.append(_sample(now, "capsule.card.review_rounds", rounds.get(task_id, 0), **lb))
        out.append(_sample(now, "capsule.card.errors", c["errors"], **lb))
        out.append(_sample(now, "capsule.card.error_repeats",
                            max(c["fp"].values()) if c["fp"] else 0, **lb))
    return out


# -------------------------------------------------------------------- power --
# Delegation ratio (M005), design section 7.2. The
# windows are 7 d and 30 d, so this reads Postgres `events` and NOT the local
# spool: cleanup.sh trims the spool to the shipped offset, so it holds no
# history (design section 4, discrepancy 7).
#
# A machine RULING is a verdict repair / the bridge
# reached without asking. Excluded on purpose: pr_ready, stuck, decision_open
# (they ask him), approved and gate_changes (they ARE him), and every
# *_error / *_failed row - a crash is not a ruling.
POWER_SQL = (
    "select 'rulings' as k, '' as l, count(*)::float from events\n"
    " where kind='card' and ts > now() - interval '7 days'\n"
    # No 'review_requested' here: it is a LEDGER kind (ledger.KINDS) and a
    # old-system `task_events.kind`, and nothing anywhere writes it as a
    # `kind='card'` event's `payload.event` - board_bridge.py:621 appends it to
    # the ledger, not to the event trail. Listing it counted nothing and made
    # the clause misdescribe what it measures.
    "   and payload->>'event' in ('created','dispatched',\n"
    "     'merged','done','abandoned','released')\n"
    "union all select 'decisions', '', count(*)::float from events\n"
    " where kind='card' and ts > now() - interval '7 days'\n"
    "   and payload->>'event' in ('decision_answered','approved','gate_changes')\n"
    # Row 201 (A1): replayed every tick from board_bridge's `retrieval_push`
    # event (30-day window - a dispatch can sit for a while before its card
    # merges), so a fresh `capsule.card.retrieval.tokens` sample exists for as
    # long as the event is inside the window: a "re-derive every tick" idiom.
    "union all select 'retrieval', board || '/' || task_id,\n"
    "     max(coalesce((payload->>'tokens_est')::float, 0)) from events\n"
    " where kind='card' and payload->>'event'='retrieval_push'\n"
    "   and ts > now() - interval '30 days'\n"
    "   and board is not null and task_id is not null\n"
    " group by 2;\n"
)


def collect_power(run, now=None):
    now = _now(now)
    argv = ["exec", "-i", paths.PG_CONTAINER, "psql", "-U", "ops", "-d", "ops",
            "-t", "-A", "--csv", "-v", "ON_ERROR_STOP=1", "-c", POWER_SQL]
    r = events.docker_run(argv, run, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "psql failed").strip()[:200])
    out = []
    for k, l, v in csv.reader(io.StringIO(r.stdout or "")):
        if k == "rulings":
            out.append(_sample(now, "capsule.delegation.rulings.7d", float(v)))
        elif k == "decisions":
            out.append(_sample(now, "capsule.delegation.decisions.7d", float(v)))
        elif k == "retrieval" and "/" in l:
            board, task_id = l.split("/", 1)
            out.append(_sample(now, "capsule.card.retrieval.tokens", float(v),
                                board=board, task_id=task_id))
    return out


# ------------------------------------------------------------------ docker --
def _parse_mem_bytes(s):
    """'123.4MiB / 7.6GiB' (docker stats MemUsage) -> used bytes, as a float."""
    used = (s or "0B").split("/")[0].strip()
    for suf, mul in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024), ("B", 1)):
        if used.endswith(suf):
            try:
                return float(used[:-len(suf)]) * mul
            except ValueError:
                return 0.0
    return 0.0


def collect_docker(run, now=None):
    """One `docker stats` call for all seven containers (cheap, and a
    container missing from its output just means "no cpu/mem row" - no
    special-casing needed), but `docker inspect` runs once PER container: a
    single multi-name `docker inspect` call exits non-zero, wholesale, the
    instant any one of the seven names does not exist - which would silently
    drop every docker sample the moment a container is stopped/removed
    Inspecting one at a time costs six extra cheap calls
    and means an absent container reports `capsule.container.up=0` (and no
    cpu/mem/restarts) without hiding its six siblings."""
    now = _now(now)
    r1 = events.docker_run(["stats", "--no-stream", "--format", "{{json .}}"], run,
                           capture_output=True, text=True, timeout=30)
    if r1.returncode != 0:
        raise RuntimeError((r1.stderr or "docker stats failed").strip()[:200])
    stats = {}
    for line in (r1.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        name = d.get("Name")
        if name in CONTAINERS:
            stats[name] = d
    out = []
    for name, d in stats.items():
        cpu = float((d.get("CPUPerc") or "0%").rstrip("%") or 0)
        mem = _parse_mem_bytes(d.get("MemUsage"))
        out.append(_sample(now, "capsule.container.cpu.pct", cpu, container=name))
        out.append(_sample(now, "capsule.container.mem.bytes", mem, container=name))
    for name in CONTAINERS:
        r2 = events.docker_run(["inspect", "-f", "{{.Name}} {{.RestartCount}} {{.State.Status}}",
                                name], run, capture_output=True, text=True, timeout=30)
        if r2.returncode != 0:
            # Absent (or otherwise uninspectable): down, and nothing else
            # about it is knowable this tick - never let it take the other
            # six containers' samples down with it.
            out.append(_sample(now, "capsule.container.up", 0.0, container=name))
            continue
        parts = (r2.stdout or "").strip().split()
        if len(parts) < 3:
            out.append(_sample(now, "capsule.container.up", 0.0, container=name))
            continue
        restarts, status = parts[1], parts[2]
        out.append(_sample(now, "capsule.container.restarts", float(restarts), container=name))
        out.append(_sample(now, "capsule.container.up", 1.0 if status == "running" else 0.0, container=name))
    return out


# ----------------------------------------------------------------- sandbox --
def collect_sandbox(run, now=None):
    """Network-attachment count per worker container (row 127, M040): what a
    `network: none` sandbox role can prove locally about itself - see
    docs/EGRESS.md for what this does and does not prove. `docker ps` lists
    every running worker once (the runner's `capsule-run-id` label, whatever
    image it runs). `docker inspect` then runs once per
    matched container - same one-at-a-time shape as collect_docker, for the
    same reason (one absent/racing container can't take the others' samples
    down with it) - and an uninspectable container reports `attached=1`:
    unknown is not "safe"."""
    now = _now(now)
    argv = ["ps", "--filter", "label=capsule-run-id", "--format", "{{.Names}}"]
    r1 = events.docker_run(argv, run, capture_output=True, text=True, timeout=30)
    if r1.returncode != 0:
        raise RuntimeError((r1.stderr or "docker ps failed").strip()[:200])
    names = [ln.strip() for ln in (r1.stdout or "").splitlines() if ln.strip()]
    out = [_sample(now, "capsule.sandbox.containers", float(len(names)))]
    for name in names:
        r2 = events.docker_run(["inspect", "-f", "{{.HostConfig.NetworkMode}}", name], run,
                               capture_output=True, text=True, timeout=30)
        if r2.returncode != 0:
            # Uninspectable (gone mid-tick, or something else): can't prove
            # it has no network, so count it as attached rather than assume
            # the safe answer.
            out.append(_sample(now, "capsule.sandbox.network.attached", 1.0, container=name))
            continue
        mode = (r2.stdout or "").strip()
        out.append(_sample(now, "capsule.sandbox.network.attached", 0.0 if mode == "none" else 1.0, container=name))
    return out


# ----------------------------------------------------------------- launchd --
def _launchd_prefix():  # the one shared resolution is bin/lib_prefix.sh
    cmd = ['. "$1"; capsule_launchd_prefix', "_", str(REPO / "bin" / "lib_prefix.sh")]
    r = subprocess.run(["bash", "-c", *cmd], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "launchd prefix unresolved")
    return r.stdout.strip()


def collect_launchd(run, state_dir, now=None, prefix=None):
    now = _now(now)
    out = []
    prefix = prefix or _launchd_prefix()
    labels = sorted(p.stem.replace("__PREFIX__", prefix, 1) for p in TEMPLATES_DIR.glob("*.plist"))
    r = run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "launchctl list failed").strip()[:200])
    loaded = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            loaded.add(parts[2])
    for label in labels:
        out.append(_sample(now, "capsule.job.loaded", 1.0 if label in loaded else 0.0, label=label))
    for stamp_path in sorted((Path(state_dir) / "jobs").glob("*.json")):
        if stamp_path.stem == "sprint-sync":
            continue  # its own collector (bridge) below
        job = stamp_path.stem
        try:
            d = json.loads(stamp_path.read_text())
        except Exception:
            continue
        if d.get("exit") is not None:
            out.append(_sample(now, "capsule.job.last_exit", float(d["exit"]), job=job))
        if d.get("duration_s") is not None:
            out.append(_sample(now, "capsule.job.last_duration_s", float(d["duration_s"]), job=job))
        end = d.get("end")
        if end:
            try:
                out.append(_sample(now, "capsule.job.age_s",
                                    (now - datetime.datetime.fromisoformat(end)).total_seconds(), job=job))
            except (ValueError, TypeError):
                pass  # a malformed `end` in one stamp must drop only that job's age_s
    return out


# ------------------------------------------------------------------ bridge --
_LEDGER_RE = re.compile(r"ledger pushed:\s*(\d+)\s*entries")


def collect_bridge(state_dir, logs_dir, now=None):
    now = _now(now)
    out = []
    d = json.loads((Path(state_dir) / "jobs" / "sprint-sync.json").read_text())
    end = d.get("end")
    if end:
        try:
            out.append(_sample(now, "capsule.bridge.tick.age_s",
                                (now - datetime.datetime.fromisoformat(end)).total_seconds()))
        except (ValueError, TypeError):
            pass  # a malformed `end` must drop only tick.age_s, not tick.exit/ledger.pushed too
    if d.get("exit") is not None:
        out.append(_sample(now, "capsule.bridge.tick.exit", float(d["exit"])))
    logs = list(Path(logs_dir).glob("sprint-sync-*.log"))
    if logs:
        newest = max(logs, key=lambda p: p.stat().st_mtime)
        n = None
        for line in newest.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _LEDGER_RE.search(line)
            if m:
                n = int(m.group(1))
        if n is not None:
            out.append(_sample(now, "capsule.bridge.ledger.pushed", float(n)))
    return out


# -------------------------------------------------------------------- heal --
def collect_heal(state_dir, now=None):
    now = _now(now)
    d = json.loads((Path(state_dir) / "heal.json").read_text())
    return [_sample(now, "capsule.heal.fails", float((e or {}).get("fails", 0) or 0), component=component)
            for component, e in d.items()]


# -------------------------------------------------------------------- host --
def collect_host(run, home, now=None):
    now = _now(now)
    r1 = run(["df", "-k", str(home)], capture_output=True, text=True, timeout=15)
    if r1.returncode != 0:
        raise RuntimeError((r1.stderr or "df failed").strip()[:200])
    lines = [l for l in (r1.stdout or "").splitlines() if l.strip()]
    if len(lines) < 2:
        raise RuntimeError("df: no data line")
    pct = None
    for f in lines[-1].split():
        if f.endswith("%"):
            pct = float(f.rstrip("%"))
            break
    if pct is None:
        raise RuntimeError("df: no capacity field")
    r2 = run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True, timeout=15)
    if r2.returncode != 0:
        raise RuntimeError((r2.stderr or "sysctl failed").strip()[:200])
    m = re.search(r"[\d.]+", r2.stdout or "")
    if not m:
        raise RuntimeError("sysctl: unparseable vm.loadavg")
    return [_sample(now, "capsule.host.disk.pct", pct),
            _sample(now, "capsule.host.load1", float(m.group(0)))]


# ---------------------------------------------------------------- collect --
def collect(run, home, state_dir, logs_dir, now=None, status=None):
    """Runs every collector, wrapped so one failure never stops the others.
    A collector that raises contributes no samples and prints one
    `obs_probe: <name> skipped: <why>` line to stderr. `status`, if given a
    list, receives one (name, ok) pair per collector - main() uses it for the
    M/7 count without duplicating the try/except here."""
    now = _now(now)
    fns = {
        "postgres": lambda: collect_postgres(run, now),
        "boards": lambda: collect_boards(home, now),
        "cards": lambda: collect_cards(home, now),
        "docker": lambda: collect_docker(run, now),
        "sandbox": lambda: collect_sandbox(run, now),
        "launchd": lambda: collect_launchd(run, state_dir, now),
        "bridge": lambda: collect_bridge(state_dir, logs_dir, now),
        "heal": lambda: collect_heal(state_dir, now),
        "host": lambda: collect_host(run, home, now),
        "power": lambda: collect_power(run, now),
    }
    samples = []
    for name in COLLECTOR_NAMES:
        try:
            samples.extend(fns[name]())
            if status is not None:
                status.append((name, True))
        except Exception as exc:
            print(f"obs_probe: {name} skipped: {exc}", file=sys.stderr)
            if status is not None:
                status.append((name, False))
    return samples


def to_csv(samples):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for s in samples:
        w.writerow([s.ts, s.service, s.name, s.value, json.dumps(s.labels, sort_keys=True)])
    return buf.getvalue()


DEFAULT_STATE_DIR = events.state_root()


def main(argv=None, run=None, home=None, state_dir=None, logs_dir=None, now=None):
    run = run or subprocess.run
    home = Path(home) if home else Path.home()
    state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
    logs_dir = Path(logs_dir) if logs_dir else host.config_dir() / "maintenance" / "logs"
    now = _now(now)
    status = []
    samples = collect(run, home, state_dir, logs_dir, now, status=status)
    ok, total = sum(1 for _, o in status if o), len(status)
    try:   # t_53c8a81a: advisory only, whatever it raises never changes the exit code
        print("obs_probe: " + obs_image_lock.check(run, home=home)[1])
    except Exception as exc:
        print(f"obs_probe: worker image n/a: {type(exc).__name__}")
    if ok == 0:
        print(f"obs_probe: 0 samples, 0/{total} collectors ok")
        return 1
    res = ship(to_csv(samples), run)
    print(f"obs_probe: {len(samples)} samples, {ok}/{total} collectors ok")
    if res.ok:
        return 0
    if res.skip_reason:
        # Door shut, not a probe error: say so on stdout, exit 0 (no page for a stack that is down).
        print(f"obs_probe: ship skipped: {res.skip_reason}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
