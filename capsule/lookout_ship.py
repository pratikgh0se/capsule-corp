"""The Lookout shipper (spec 5.1, 5.3, 5.8, 5.10, 11.2, 11.4): spool -> ops-pg."""
import base64
import csv
import datetime
import fcntl
import http.client
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from capsule import db, legacy_names, lookout, obs_envelope, paths

sys.path.insert(0, str(paths.REPO / "maintenance"))
import events  # noqa: E402  (docker_env / docker_bin / ship: the one transport, G1)
import scrub  # noqa: E402

CONTAINER = paths.PG_CONTAINER
TIMEOUT = 30
# psql itself exits 3 on any SQL error under -v ON_ERROR_STOP=1; EXIT_SPOOL_BAD is the shipper's
# own, distinct code for a spool line this version cannot parse, so the two never collide.
EXIT_SPOOL_BAD = 5
UNPRICED_JOB = "lookout-unpriced"
QUARANTINE_JOB = "lookout-quarantine"  # the job name rules.yaml `lookout-quarantined` reads
# Bounded chunks instead of one psql call for up to SPOOL_MAX_BYTES (64 MB) - whichever bound
# (rows or bytes) is hit first ends the chunk.
CHUNK_ROWS = 2000
CHUNK_BYTES = 4 * 1024 * 1024
PRICES_EVERY = 3600
SILENCE_MAX = 12 * 3600
LAG_MAX_BYTES = 20 * 1024 * 1024        # `check` fails past this many unshipped bytes
SEVERITY = {"info": "INFO", "warn": "WARN", "error": "ERROR"}
FREE_TEXT = ("title", "text", "block_reason", "reason", "root_cause")
ENVELOPE = ("board", "snap_ts", "pass_id")
TABLES = {
    "cap_cards": (("board", "task_id"), ENVELOPE + (
        "task_id", "title", "status", "product", "epic", "repo", "phase", "role_now", "model_now", "run_id_now",
        "block_kind", "block_reason", "waiting_on", "waiting_words", "waiting_since", "failure_count",
        "consecutive_failures", "risk_class", "tier", "created_at", "started_at", "completed_at", "commits",
        "review_rounds")),
    "cap_runs": (("board", "run_id"), ENVELOPE + (
        "run_id", "task_id", "role", "model", "provider", "account", "runner", "status", "outcome", "error_class",
        "attempts", "started_at", "ended_at", "last_heartbeat_at", "tokens_in", "tokens_out", "tokens_cache_read",
        "tokens_cache_write", "era")),
    "cap_blocks": (("board", "task_id", "opened_at"), ENVELOPE + (
        "task_id", "opened_at", "kind", "reason", "waiting_on", "opened_event_id", "closed_at", "closed_by")),
    "cap_epics": (("board", "epic"), ENVELOPE + (
        "epic", "product", "repo", "spec", "decision_rows", "cards_total", "cards_done", "cards_blocked",
        "head_sha", "ahead_of_main", "pr_number", "pr_url", "publish_state", "publish_reason",
        "publish_state_since")),
    "cap_prs": (("repo", "pr_number"), ENVELOPE + (
        "repo", "pr_number", "epic", "url", "state", "checks", "review_decision", "head_sha", "cards_in_pr",
        "opened_at", "ready_at", "merged_at", "merge_sha", "closed_at", "updated_at")),
    "cap_commits": (("repo", "sha"), ENVELOPE + (
        "repo", "sha", "epic", "task_id", "run_id", "committed_at", "validator", "reviewer", "guide",
        "review_rounds", "overrides", "pushed_at", "pr_number", "pr_comment_url")),
    "cap_incidents": (("board", "id"), ENVELOPE + (
        "id", "task_id", "run_id", "kind", "status", "role", "opened_at", "closed_at", "root_cause", "fix_card",
        "repeat_of")),
    "cap_tick": (("board",), ENVELOPE + (
        "host", "pid", "harness_sha", "last_pass_at", "pass_ms", "lookout_ms", "counts", "running", "ready",
        "blocked", "paused_accounts", "spool_bytes_unshipped")),
    "events": (("event_id",), ("v", "event_id", "ts", "kind", "source", "board", "task_id", "slug", "title",
                               "text", "link", "severity", "payload",
                               "event_name", "run_id", "trace_id", "span_id")),
}
STAGE_ORDER = ("cap_cards", "cap_runs", "cap_blocks", "cap_epics", "cap_prs", "cap_commits", "cap_incidents",
               "cap_tick", "events")


class SpoolBad(ValueError):
    pass


def _with_envelope(doc):
    """OBS-09: build the events row through the factory envelope; the new columns come from it.
    problems() is the pre-ship check: a malformed record is flagged on stderr and ships with
    null envelope columns, so one odd record never blocks the spool behind it."""
    pay = doc.get("payload")
    pay = pay if isinstance(pay, dict) else {}
    kind = doc.get("kind")
    name = pay.get("event") if kind == "card" else obs_envelope.KIND_NAMES.get(kind)
    name = name if isinstance(name, str) else None
    resource = {"service.name": "capsule-corp", "service.version": f"spool-v{lookout.SPOOL_V}",
                "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
                "deployment.environment.name": "local"}
    env = obs_envelope.envelope(
        name or "unmapped", doc.get("text") or doc.get("title") or doc.get("kind") or "-", resource,
        severity=SEVERITY.get(doc.get("severity"), "INFO"), board=doc.get("board"),
        task_id=doc.get("task_id"), run_id=pay.get("run_id"), step=doc.get("event_id"))
    bad = obs_envelope.problems(env)
    if bad:
        print(f"lookout ship: envelope: {doc.get('event_id')}: {'; '.join(bad)}", file=sys.stderr)
        return dict(doc, event_name=None, run_id=None, trace_id=None, span_id=None)
    return dict(doc, event_name=env["event.name"], run_id=env.get("capsule.run_id"),
                trace_id=env.get("trace_id"), span_id=env.get("span_id"))


def _scrubbed(row):
    return {k: (scrub.scrub(v) if k in FREE_TEXT and isinstance(v, str) else v) for k, v in row.items()}


def parse(raw):
    """One spool line -> (table, doc). Raises SpoolBad on anything this version does not know."""
    try:
        line = json.loads(raw)
    except ValueError as exc:
        raise SpoolBad(f"not json: {exc}")
    if not isinstance(line, dict) or line.get("v") != lookout.SPOOL_V:
        raise SpoolBad(f"unknown v {line.get('v') if isinstance(line, dict) else None!r}")
    t, row = line.get("t"), line.get("row")
    if not isinstance(row, dict):
        raise SpoolBad("row is not an object")
    if t == "event":
        table, doc = "events", dict(row)
        doc = _with_envelope(doc)
    elif t == "snap":
        table = line.get("table")
        if table not in TABLES or table in ("events", "cap_tick"):
            raise SpoolBad(f"unknown table {table!r}")
        doc = dict(row, board=line.get("board"), snap_ts=line.get("snap_ts"), pass_id=line.get("pass_id"))
    elif t == "tickmark":
        table = "cap_tick"
        doc = dict(row, board=line.get("board"), snap_ts=line.get("snap_ts"), pass_id=line.get("pass_id"))
    else:
        raise SpoolBad(f"unknown t {t!r}")
    unknown = set(doc) - set(TABLES[table][1])
    if unknown:
        raise SpoolBad(f"unknown keys for {table}: {sorted(unknown)}")
    return table, _scrubbed(doc)


def _upsert(table):
    keys, cols = TABLES[table]
    col_list = ", ".join(cols)
    if table == "events":
        return (f"insert into events ({col_list}) select {', '.join('r.' + c for c in cols)} from lookout_stage s"
                f" cross join lateral jsonb_populate_record(null::events, s.doc) r where s.tbl = 'events'"
                f" order by s.seq on conflict (event_id) where event_id is not null do nothing;\n")
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys)
    return (f"insert into {table} ({col_list}) select {col_list} from (select distinct on ({', '.join(keys)})"
            f" r.*, s.seq from lookout_stage s cross join lateral jsonb_populate_record(null::{table}, s.doc) r"
            f" where s.tbl = '{table}' order by {', '.join(keys)}, seq desc) x"
            f" on conflict ({', '.join(keys)}) do update set {sets} where excluded.snap_ts >= {table}.snap_ts;\n")


def _lit(v):
    return "null" if v is None else ("'" + str(v).replace("'", "''") + "'")


def build_script(lines, *, left, prices, tables=None):
    """lines: [(table, doc)] in spool order. left: {board: unshipped bytes after this ship}."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for seq, (table, doc) in enumerate(lines):
        w.writerow([seq, table, json.dumps(doc, ensure_ascii=False)])
    body = buf.getvalue().replace("\r\n", "\n")
    # so a failed script's ERROR line carries its SQLSTATE code
    out = ["\\set VERBOSITY verbose\n",
           "BEGIN;\n",
           "create temp table lookout_stage (seq int, tbl text, doc jsonb) on commit drop;\n",
           "COPY lookout_stage (seq, tbl, doc) FROM STDIN WITH (FORMAT csv);\n", body, "\\.\n"]
    for table in tables or STAGE_ORDER:
        out.append(_upsert(table))
    for p in prices or []:
        out.append("insert into prices (model, valid_from, usd_in_per_mtok, usd_out_per_mtok,"
                   " usd_cache_read_per_mtok, usd_cache_write_per_mtok) values"
                   f" ({_lit(p['model'])}, {_lit(p['valid_from'])}, {p['in']}, {p['out']}, {p['cache_read']},"
                   f" {'null' if p.get('cache_write') is None else p['cache_write']})"
                   " on conflict (model, valid_from) do update set usd_in_per_mtok = excluded.usd_in_per_mtok,"
                   " usd_out_per_mtok = excluded.usd_out_per_mtok,"
                   " usd_cache_read_per_mtok = excluded.usd_cache_read_per_mtok,"
                   " usd_cache_write_per_mtok = excluded.usd_cache_write_per_mtok;\n")
    for board, n in sorted(left.items()):
        out.append(f"update cap_tick set last_ship_ok_at = now(), spool_bytes_unshipped = {int(n)}"
                   f" where board = {_lit(board)};\n")
    out.append("COMMIT;\n")
    return "".join(out)


def psql(script, *, timeout=TIMEOUT, tuples=False):
    argv = ["exec", "-i", CONTAINER, "psql", "-U", "ops", "-d", "ops", "-q",
            "-v", "ON_ERROR_STOP=1"]
    if tuples:
        argv += ["-t", "-A", "-F", "|"]
    try:
        r = events.docker_run(argv + ["-f", "-"], input=script, text=True, capture_output=True,
                              timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "psql timed out"
    except OSError as exc:
        return 127, str(exc)


def _prices():
    doc = json.loads((paths.REPO / "maintenance" / "prices.json").read_text(encoding="utf-8"))
    return [dict(v, model=m) for m, v in doc["models"].items()]


def _last(home, board=None):
    try:
        return json.loads((lookout.lookout_dir(home, board) / "ship.last.json").read_text())
    except (OSError, ValueError):
        return {}


def _chunks(n, spans):
    """[(lo, hi)) index ranges into `lines`/`spans`, bounded by CHUNK_ROWS or CHUNK_BYTES,
    whichever is hit first (item 2). Every chunk holds at least one line."""
    i = 0
    while i < n:
        j, nbytes = i, 0
        while j < n:
            span_len = spans[j][1] - spans[j][0]
            if j > i and (j - i >= CHUNK_ROWS or nbytes + span_len > CHUNK_BYTES):
                break
            nbytes += span_len
            j += 1
        yield i, j
        i = j


# SQLSTATEs are 5 chars from [0-9A-Z], not all-digit
_SQLSTATE_RE = re.compile(r"ERROR:\s+([0-9A-Z]{5}):")
_RECOVERABLE_CLASSES = ("22", "23")   # 22 data exception, 23 integrity constraint violation


def _recoverable(out):
    """A chunk's failure is only ever row-shaped - safe to isolate
    to one spool row - when psql's own verbose message names SQLSTATE class 22 (a bad
    timestamp/int/ array through jsonb_populate_record) or 23 (NOT
    NULL/check/unique/FK)."""
    m = _SQLSTATE_RE.search(out or "")
    return m is not None and m.group(1)[:2] in _RECOVERABLE_CLASSES


def _quarantine(home, bad, board=None):
    """Spool rows Postgres rejects on a real constraint or data-exception error land in
    spool.rejected."""
    p = lookout.rejected_path(home, board)
    seen = set()
    if p.exists():
        with open(p, encoding="utf-8") as f:
            seen = {json.loads(l).get("line") for l in f if l.strip()}
    for span, raw_line, err in bad:
        if raw_line in seen:
            continue
        seen.add(raw_line)
        entry = json.dumps({"ts": time.time(), "span": f"{span[0]}:{span[1]}", "line": raw_line,
                            "error": (err or "").strip()[:2000]}, ensure_ascii=False) + "\n"
        lookout.rotate_rejected(home, len(entry.encode("utf-8")), board)
        with open(p, "a", encoding="utf-8") as f:
            f.write(entry)


def _ship_chunk(home, chunk_lines, chunk_spans, chunk_raws, *, left, prices, board=None):
    """Ship one bounded chunk (item 2)."""
    rc, out = psql(build_script(chunk_lines, left=left, prices=prices), timeout=TIMEOUT)
    first_out = out
    if rc == 0:
        return 0, 0, prices is not None, out
    if rc != 3 or not chunk_lines or not _recoverable(out):
        return rc, 0, False, out
    if prices is not None:
        rc, out = psql(build_script(chunk_lines, left=left, prices=None), timeout=TIMEOUT)
        if rc == 0:
            return 0, 0, False, first_out      # prices alone were the problem; every row is fine
        if rc != 3 or not _recoverable(out):
            return rc, 0, False, out            # a genuine failure unrelated to any specific row
    bad = []
    for line, span, raw in zip(chunk_lines, chunk_spans, chunk_raws):
        r2, out2 = psql(build_script([line], left=left, prices=None), timeout=TIMEOUT)
        if r2 == 3 and _recoverable(out2):
            bad.append((span, raw, out2))
        elif r2 != 0:
            return r2, 0, False, out2          # a genuine non-recoverable failure while isolating
    if not bad or len(bad) == len(chunk_lines):
        # every row failed, or none did: never row-shaped.
        return rc, 0, False, out
    _quarantine(home, bad, board)
    # the prices error (if prices rode) is the one worth quoting
    return 0, len(bad), False, first_out


def _parse_spool(data, start):
    """Parse every whole line from `start` to the first this version cannot read."""
    lines, spans, raws, end, bad, pos = [], [], [], start, None, start
    for raw in data[start:].split(b"\n")[:-1]:         # the last piece is a partial line or b""
        line_start = pos
        try:
            lines.append(parse(raw.decode("utf-8")))
        except (SpoolBad, UnicodeDecodeError) as exc:
            bad = str(exc)
            break
        pos += len(raw) + 1
        end = pos
        spans.append((line_start, pos))
        raws.append(raw.decode("utf-8", "replace"))
    return lines, spans, raws, end, bad


def _warn_unpriced(lines):
    """A run whose model has no prices.json row costs null in v_run_cost, so say which models."""
    known = {p["model"] for p in _prices()}
    models = sorted({doc.get("model") or "(none)" for table, doc in lines
                     if table == "cap_runs" and (doc.get("model") or "(none)") not in known})
    for model in models:
        print(f"lookout ship: no price for model {model}: its runs cost null", file=sys.stderr)
        events.log("job", "lookout", f"lookout ship: no price for model {model}, runs cost null",
                   severity="warn",
                   payload={"job": UNPRICED_JOB, "phase": "end", "exit": 0, "model": model})


def _ship_chunks(home, op, data, lines, spans, raws, *, start, due, board=None):
    """Item 2: ship every bounded chunk in spool order, advancing `op` (the offset file)
    after each one lands - not only once at the very end."""
    _warn_unpriced(lines)
    prices_pending = _prices() if due else None
    quarantined, prices_shipped, out, cursor = 0, False, "", start
    for lo, hi in _chunks(len(lines), spans) if lines else [(0, 0)]:
        chunk_lines, chunk_spans, chunk_raws = lines[lo:hi], spans[lo:hi], raws[lo:hi]
        chunk_end = spans[hi - 1][1] if hi > lo else cursor
        boards = {doc.get("board") for _, doc in chunk_lines if doc.get("board")}
        left = {b: len(data) - chunk_end for b in boards}
        rc, cq, cp, out = _ship_chunk(home, chunk_lines, chunk_spans, chunk_raws, left=left,
                                      prices=prices_pending, board=board)
        if rc != 0:
            return rc, out, quarantined, prices_shipped
        if cp:
            prices_shipped, prices_pending = True, None
        elif prices_pending is not None and lines:
            print(f"lookout ship: prices failed, rows shipped without them: {out.strip()[:300]}", file=sys.stderr)
        quarantined += cq
        cursor = chunk_end
        op.write_text(str(cursor))                     # item 2: advance the offset per chunk
    return 0, out, quarantined, prices_shipped


def _announce_quarantine(home, quarantined, events_spool, board=None):
    """A quarantine exits 0, so say so twice: one stderr line for the tick log, and one `job` event
    (exit 0, so `job-failed` stays quiet) that ships to ops-pg, where the `lookout-quarantined`
    Grafana notice reads it."""
    rejected = lookout.rejected_path(home, board)
    print(f"lookout ship: quarantined {quarantined} row(s) to {rejected}", file=sys.stderr)
    events.log("job", "lookout", f"lookout ship: quarantined {quarantined} spool row(s) to spool.rejected",
               severity="warn", spool=events_spool,
               payload={"job": QUARANTINE_JOB, "phase": "end", "exit": 0, "quarantined": quarantined})


def _ship_pending(home, board, parsed, *, start, last, due, events_spool):
    """Ship what _parse_spool found, note the outcome in the dir's ship.last.json; return rc."""
    lines, spans, raws, data = parsed
    op = lookout.offset_path(home, board)
    rc, out, quarantined, prices_shipped = _ship_chunks(
        home, op, data, lines, spans, raws, start=start, due=due, board=board)
    if rc != 0:
        print(f"lookout ship: psql rc {rc}: {out.strip()[:300]}", file=sys.stderr)
        new_last = dict(last, ts=time.time(), rc=rc, lines_shipped=0, quarantined=0)
    else:
        if quarantined:
            _announce_quarantine(home, quarantined, events_spool, board)
        new_last = {"ts": time.time(), "rc": 0, "lines_shipped": len(lines) - quarantined,
                    "quarantined": quarantined,
                    "prices_at": time.time() if prices_shipped else last.get("prices_at")}
    (lookout.lookout_dir(home, board) / "ship.last.json").write_text(json.dumps(new_last))
    return rc


def _ship_one(home, board, *, full, events_spool):
    """Ship one spool dir (the shared one for board None) under its own ship.lock and offset."""
    d = lookout.lookout_dir(home, board)
    d.mkdir(parents=True, exist_ok=True)
    if full:
        (d / "resync.want").write_text(str(int(time.time())))
    with open(d / "ship.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0                                   # another shipper owns the spool
        sp, op = lookout.spool_path(home, board), lookout.offset_path(home, board)
        data = sp.read_bytes() if sp.exists() else b""
        try:
            start = int(op.read_text())
        except (OSError, ValueError):
            start = 0
        if start > len(data):
            start = 0
        lines, spans, raws, end, bad = _parse_spool(data, start)
        last = _last(home, board)
        due = full or time.time() - float(last.get("prices_at") or 0) >= PRICES_EVERY
        rc = 0
        if lines or due:
            parsed = (lines, spans, raws, data)
            rc = _ship_pending(home, board, parsed, start=start, last=last, due=due,
                               events_spool=events_spool)
        if bad is not None:
            print(f"lookout_spool_bad at byte {end}: {bad}", file=sys.stderr)
        return rc or (EXIT_SPOOL_BAD if bad is not None else 0)


def spool_boards(home):
    """Every spool owner under lookout/: None for the shared (legacy tick) spool, then each board
    directory holding a spool.jsonl, sorted."""
    root = lookout.lookout_dir(home)
    return ([None] if (root / "spool.jsonl").exists() else []) + sorted(
        p.parent.name for p in root.glob("*/spool.jsonl"))


def ship(home, *, full=False, events_spool=None):
    home = Path(home)
    if os.environ.get("AGENT_OPS_NO_SEND") and not os.environ.get("AGENT_OPS_DOCKER_BIN"):
        print("lookout ship: AGENT_OPS_NO_SEND and no docker override - not sending",
              file=sys.stderr)
        return 0
    root = lookout.lookout_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "ship.run.lock", "a") as run:
        try:
            fcntl.flock(run, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0                                   # another shipper run is live: touch nothing
        (root / "ship.pid").write_text(str(os.getpid()))   # the tick's single-flight
        rc = 0
        try:
            for board in spool_boards(home) or [None]:
                rc = max(rc, _ship_one(home, board, full=full, events_spool=events_spool))
        finally:
            events.ship()                          # the maintenance spool keeps flowing (5.1)
            # The retired bridge also called events.ship() on this offset file; rows carry
            # an event_id and the insert is ON CONFLICT DO NOTHING, so a race only re-copies.
        return rc


def status(home, board=None):
    home = Path(home)
    sp, op = lookout.spool_path(home, board), lookout.offset_path(home, board)
    size = sp.stat().st_size if sp.exists() else 0
    try:
        offset = int(op.read_text())
    except (OSError, ValueError):
        offset = 0
    lock = lookout.lookout_dir(home, board) / "ship.lock"
    alive = False
    if lock.exists():
        with open(lock) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                alive = True                           # another shipper holds ship.lock
    rp = lookout.rejected_path(home, board)
    # rotation may remove the sidecar between a stat and the read: a cosmetic count, never a crash
    try:
        rejected = sum(1 for l in rp.read_text(encoding="utf-8").splitlines() if l.strip())
    except OSError:
        rejected = 0
    return {"spool_bytes": size, "offset": offset, "unshipped_bytes": max(0, size - offset),
            "shipper_running": alive, "last": _last(home, board), "rejected": rejected}


def unshipped(home):
    """Bytes not yet shipped, summed over the shared spool and every board's own."""
    return sum(status(home, b)["unshipped_bytes"] for b in spool_boards(home))


def _sqlite_counts(conn, now):
    mark, _ = db.guard_get(conn, lookout.EVENTS_WATERMARK)
    top = (mark or {}).get("event_id", 0)
    horizon = now - 170 * 86400
    # Open blocks by the board's own state, independent of
    # lookout.BLOCK_OPEN/BLOCK_CLOSE, so this stays a second oracle: blocked, parked or
    # looping (blocked/triage), waits on a parent (todo + dependency), or holds its
    # epic's first screen (guard_state first_screen:<branch> held).
    held = set()
    for (value,) in conn.execute("SELECT value FROM guard_state WHERE key LIKE 'first_screen:%'"):
        doc = json.loads(value)
        if isinstance(doc, dict) and doc.get("state") == "held":
            held.add(doc.get("task_id"))
    # A guide ruling in progress parks the card in triage with no block event, so it is no open
    # block
    # on either side; a transient block is (collect opens it from its `blocked` event).
    open_blocks = len({tid for (tid,) in conn.execute(
        "SELECT id FROM tasks WHERE (status IN ('blocked', 'triage') AND COALESCE(block_kind, '') <> 'guide_ruling')"
        " OR (status = 'todo' AND block_kind = 'dependency')")} | held)
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]   # noqa: E731
    return {"cards": q("SELECT COUNT(*) FROM tasks"),
            "runs": q("SELECT COUNT(*) FROM task_runs"),                     # both eras (A3)
            "runs_without_run_id": q("SELECT COUNT(*) FROM task_runs WHERE run_id IS NULL"),
            "events": q("SELECT COUNT(*) FROM task_events WHERE kind <> 'heartbeat' AND id <= ? AND created_at > ?",
                        top, horizon),
            "open_blocks": open_blocks,
            "open_incidents": q("SELECT COUNT(*) FROM incidents"
                                " WHERE status NOT IN ('closed','superseded')"),
            "commits": q("SELECT COUNT(*) FROM task_events WHERE kind='completed'"
                         " AND json_extract(payload, '$.commit') IS NOT NULL")}


def stalled(home):
    """Why shipping has silently stopped, or None: over LAG_MAX_BYTES unshipped in total, or
    any one spool with bytes waiting and no good ship in SILENCE_MAX (never shipped counts).
    Ignores the config flag."""
    lag = unshipped(home)
    if lag > LAG_MAX_BYTES:
        return f"{lag} bytes unshipped (limit {LAG_MAX_BYTES})"
    for b in spool_boards(home):
        waiting, ts = status(home, b)["unshipped_bytes"], _last(home, b)
        good = ts.get("rc") == 0 and isinstance(ts.get("ts"), (int, float))
        age = time.time() - ts["ts"] if good else None
        if waiting > 0 and (age is None or age > SILENCE_MAX):
            when = "never shipped" if age is None else f"last ship {int(age)}s ago"
            return f"{waiting} bytes unshipped in spool {b or 'shared'} and {when}"
    return None


def check(home, cfg, *, events_spool=None):
    """Spec 11.2: never repairs; a mismatch is one `job` event with exit 1 (job-failed pages).
    While the spool still holds unshipped bytes, ops-pg is behind by design, so the comparison is
    skipped with a notice instead of paging on lag."""
    board = cfg["board"]
    stale = stalled(home)
    lag = unshipped(home)
    if stale:
        print(f"lookout check: shipping stalled: {stale}", file=sys.stderr)
        events.log("job", "lookout", f"lookout check: shipping stalled, {stale}", board=board,
                   severity="error", payload={"job": "lookout-check", "phase": "end", "exit": 1,
                                              "stalled": stale}, spool=events_spool)
        return 1
    if lag > 0:
        print(f"lookout check: skipped, {lag} bytes not shipped yet (Postgres is behind by design)")
        return 0
    conn = db.connect_ro(paths.board_db(board, Path(home)))
    try:
        mine = _sqlite_counts(conn, time.time())
    finally:
        conn.close()
    b = _lit(board)
    sql = (f"select 'cards', count(*) from cap_cards where board = {b} union all "
           f"select 'runs', count(*) from cap_runs where board = {b} union all "
           f"select 'runs_without_run_id', count(*) from cap_runs where board = {b} "
           f"and run_id like '{legacy_names.RUN_ID_PREFIX}%' union all "
           f"select 'events', count(*) from events where kind = 'card' and source = 'capsule' and board = {b}"
           f" and payload->>'event' <> 'archived' and ts > now() - interval '170 days' union all "
           f"select 'open_blocks', count(*) from cap_blocks where board = {b} and closed_at is null union all "
           f"select 'open_incidents', count(*) from cap_incidents where board = {b}"
           f" and status not in ('closed','superseded') union all "
           f"select 'commits', count(*) from cap_commits where board = {b};\n")
    rc, out = psql(sql, timeout=TIMEOUT, tuples=True)
    if rc != 0:
        print(f"lookout check: psql rc {rc}: {out.strip()[:300]}", file=sys.stderr)
        return 2
    theirs = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        k, v = line.split("|", 1)
        try:
            theirs[k.strip()] = int(v)
        except ValueError:                    # a stray psql notice or an empty count, not a crash
            print(f"lookout check: psql: unreadable count line {line!r}", file=sys.stderr)
            return 2
    print(f"lookout check: A3 legacy-era runs without a run_id: "
          f"{mine['runs_without_run_id']} on the board,"
          f" {theirs.get('runs_without_run_id')} shipped as {legacy_names.RUN_ID_PREFIX}<id>")
    diff = {k: (mine[k], theirs.get(k)) for k in mine if theirs.get(k) != mine[k]}
    for k, (a, b2) in diff.items():
        print(f"lookout check: {k} sqlite={a} ops-pg={b2}")
    if diff:
        events.log("job", "lookout", "lookout check: mismatch " + ", ".join(sorted(diff)), board=board,
                   severity="error", payload={"job": "lookout-check", "phase": "end", "exit": 1,
                                              "mismatches": {k: list(v) for k, v in diff.items()}},
                   spool=events_spool)
        return 1
    print("lookout check: equal")
    return 0


def _duration(text):
    m = re.fullmatch(r"(\d+)([smh])", text or "")
    if not m:
        return None
    s = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    return s if 0 < s <= SILENCE_MAX else None


def silence(duration):
    """Spec 11.4 / A4: an expiring Grafana silence, capped at 12 h, on both rules a deliberate tick
    stop trips: capsule-tick-deadman (the dead-tick page, ruling a) and factory-not-reporting."""
    secs = _duration(duration)
    if secs is None:
        print("lookout silence: needs --for <N>s|m|h, more than 0 and at most 12h (a silence must end)",
              file=sys.stderr)
        return 2
    secret_bin = os.environ.get("CAPSULE_SECRET_BIN") or str(paths.REPO / "bin" / "capsule-secret")
    r = subprocess.run([secret_bin, "get", "capsule-corp", "OPS_GRAFANA_PASSWORD"], capture_output=True, text=True,
                       timeout=30, env=dict(os.environ, CAPSULE_IDENTITY="compose"))
    if r.returncode != 0 or not r.stdout.strip():
        print("lookout silence: could not read the Grafana password through capsule-secret", file=sys.stderr)
        return 1
    auth = base64.b64encode(f"admin:{r.stdout.strip()}".encode()).decode()
    now = datetime.datetime.now(datetime.timezone.utc)
    body = json.dumps({"matchers": [{"name": "__alert_rule_uid__",
                                     "value": "capsule-tick-deadman|factory-not-reporting",
                                     "isRegex": True, "isEqual": True}],
                       "startsAt": now.isoformat(), "endsAt": (now + datetime.timedelta(seconds=secs)).isoformat(),
                       "createdBy": "capsule lookout silence", "comment": "deliberate tick stop"})
    url = urllib.parse.urlparse(os.environ.get("LOOKOUT_GRAFANA_URL") or "http://127.0.0.1:3000")
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=10)
    try:
        conn.request("POST", "/api/alertmanager/grafana/api/v2/silences", body=body,
                     headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()
    if not 200 <= resp.status < 300:      # Grafana 11.2's RouteCreateSilence answers 202, not 200
        print(f"lookout silence: grafana answered {resp.status}", file=sys.stderr)
        return 1
    print(f"lookout silence: capsule-tick-deadman and factory-not-reporting silenced until {(now + datetime.timedelta(seconds=secs)).isoformat()}"
          f" {text[:80]}")
    return 0
