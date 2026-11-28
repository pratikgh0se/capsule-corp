"""The board store: the old system's schema plus Capsule's additive columns and tables (spec
§6.1, rows 376, 391)."""
import contextlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

from capsule import event_kinds
from capsule.answer_base import _wal_empty_or_absent, connect_ro_tolerant  # noqa: F401

BOARD_DDL = (Path(__file__).with_name("board_schema.sql")).read_text()

ADDED_COLUMNS = (
    ("tasks", "failure_count", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "failure_reset_at", "INTEGER"),
    ("tasks", "risk_class", "TEXT"),
    ("tasks", "tier", "TEXT"),
    ("tasks", "file_set", "TEXT"),
    ("tasks", "short_name", "TEXT"),
    ("tasks", "number", "INTEGER"),
    ("tasks", "name_source", "TEXT"),
    ("tasks", "contract_ref", "TEXT"),
    ("tasks", "request_needs", "TEXT"),
    # card fields (R04): all NULL is legacy behaviour; never reuse `risk_class` (runner tier)
    ("tasks", "check_cmd", "TEXT"),
    ("tasks", "check_expect", "TEXT"),
    ("tasks", "size_tag", "TEXT"),
    ("tasks", "risk_flags", "TEXT"),
    ("tasks", "passes", "INTEGER"),
    ("tasks", "workload", "TEXT"),
    # inc_b862eccd: the epic this dependency wait names;
    # cleared automatically when the epic lands (blocks.py promote_ready)
    ("tasks", "waits_on_branch", "TEXT"),
    ("tasks", "blind_commit", "TEXT"),    # R31: the blind commit whose tests landed
    ("task_runs", "run_id", "TEXT"),
    ("task_runs", "provider", "TEXT"),
    ("task_runs", "account", "TEXT"),
    ("task_runs", "model", "TEXT"),
    ("task_runs", "runner", "TEXT"),
    ("task_runs", "tokens_in", "INTEGER"),
    ("task_runs", "tokens_out", "INTEGER"),
    ("task_runs", "tokens_cache_read", "INTEGER"),
    ("task_runs", "tokens_cache_write", "INTEGER"),
    ("task_runs", "handoff", "TEXT"),
    ("task_runs", "resumed_from", "TEXT"),
    ("task_runs", "attempts", "INTEGER"),
    ("task_runs", "error_class", "TEXT"),
    ("task_runs", "session_id", "TEXT"),    # the model session a quiet stop resumes
)

# The incidents table's column list, without its own `CREATE TABLE <name> (` wrapper or
# CHECK constraints, so `ADDED_TABLES` (a fresh board) and `_widen_incidents_kind` (an
# existing board's rebuild) share exactly one literal - never two copies that could
# drift apart.
INCIDENTS_KIND = ("crash", "hang", "silent_failure", "review_infra", "invalid_document",
                  "contradiction", "human_intervention", "override", "escaped_bug", "repeat",
                  "two_dispatchers", "main_checkout_write", "lost_card_state", "runaway")
INCIDENTS_STATUS = ("open", "postmortem", "fix_pending", "closed", "superseded")
INCIDENTS_COLUMNS = ("id", "board", "task_id", "run_id", "kind", "opened_at", "evidence", "root_key",
                     "root_cause", "lesson", "mitigation", "enforcer", "recurrence_test",
                     "decision_row", "decision_row_text", "confidence", "fingerprint", "status",
                     "closed_at", "repeat_of", "fix_card", "korin_task", "note_path", "role",
                     "main_sha", "closure", "description")


def _incidents_body():
    kind = ",".join(f"'{k}'" for k in INCIDENTS_KIND)
    status = ",".join(f"'{s}'" for s in INCIDENTS_STATUS)
    return (
        "id TEXT PRIMARY KEY, board TEXT NOT NULL, task_id TEXT, run_id TEXT,\n"
        f"  kind TEXT NOT NULL CHECK (kind IN ({kind})),\n"
        "  opened_at INTEGER NOT NULL, evidence TEXT NOT NULL DEFAULT '[]', root_key TEXT,\n"
        "  root_cause TEXT, lesson TEXT, mitigation TEXT, enforcer TEXT, recurrence_test TEXT,\n"
        "  decision_row INTEGER, decision_row_text TEXT, confidence TEXT, fingerprint TEXT NOT NULL,\n"
        f"  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ({status})),\n"
        "  closed_at INTEGER, repeat_of TEXT, fix_card TEXT, korin_task TEXT, note_path TEXT, role TEXT,\n"
        "  main_sha TEXT, closure TEXT, description TEXT"
    )


ADDED_TABLES = f"""
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, thread_key TEXT,
  body TEXT NOT NULL, created_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  delivered_at INTEGER, last_error TEXT, status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS outbox_key (
  outbox_id INTEGER PRIMARY KEY, dedup_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slack_msg (
  key TEXT PRIMARY KEY, kind TEXT NOT NULL, channel TEXT NOT NULL, ts TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open', text TEXT NOT NULL, posted_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, reminded_at INTEGER);
CREATE TABLE IF NOT EXISTS guard_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS thread_owner (
  channel TEXT NOT NULL, thread_ts TEXT NOT NULL, task_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (channel, thread_ts));
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, claim_lock TEXT, verb TEXT,
  schema TEXT, body TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created_at INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_run_id ON task_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_branch_name ON tasks(branch_name);
CREATE INDEX IF NOT EXISTS idx_task_events_run_id ON task_events(run_id);
CREATE TABLE IF NOT EXISTS incidents (
  {_incidents_body()});
CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(fingerprint, opened_at);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE TABLE IF NOT EXISTS app_event_seen (
  token TEXT PRIMARY KEY, incident_id TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_app_event_seen_at ON app_event_seen(at);
"""


def connect(path):
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=120)
    conn.row_factory = sqlite3.Row
    for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL",
                   "PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=120000"):
        conn.execute(pragma)
    return conn


def connect_ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=120)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def txn(conn):
    """BEGIN IMMEDIATE ... COMMIT; inside an open transaction it is a SAVEPOINT."""
    if conn.in_transaction:
        name = "sp_" + secrets.token_hex(4)
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


INCIDENTS_WATERMARK = "incidents:watermark"


def _incidents_missing(conn):
    """The nullable columns (`closure`: how Korin closed or superseded it: watch, history,
    duplicate, watch_failed; `description`: an operator's prose) an existing
    `incidents` table lacks; none when the table is absent."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(incidents)")}
    return [c for c in ("closure", "description") if have and c not in have]


def _incidents_kind_needs_widening(conn):
    """True when `incidents` already exists but its `kind` CHECK is missing one of
    `INCIDENTS_KIND` (row 507(b): `review_infra`, `invalid_document`) or its `status` CHECK one of
    `INCIDENTS_STATUS` (`superseded`, a failed watch)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='incidents'").fetchone()
    return (row is not None and row[0] is not None
            and any(f"'{k}'" not in row[0] for k in INCIDENTS_KIND + INCIDENTS_STATUS))


def _widen_incidents_kind(conn):
    """SQLite has no `ALTER TABLE ..."""
    cols = ", ".join(INCIDENTS_COLUMNS)
    conn.execute(f"CREATE TABLE incidents_new ({_incidents_body()})")
    conn.execute(f"INSERT INTO incidents_new ({cols}) SELECT {cols} FROM incidents")
    conn.execute("DROP TABLE incidents")
    conn.execute("ALTER TABLE incidents_new RENAME TO incidents")


def migrate(conn):
    with txn(conn):
        for table, col, decl in ADDED_COLUMNS:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        for col in _incidents_missing(conn):    # before any widening: its copy names them
            conn.execute(f"ALTER TABLE incidents ADD COLUMN {col} TEXT")
        if _incidents_kind_needs_widening(conn):
            _widen_incidents_kind(conn)
    had_incidents = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidents'").fetchone() is not None
    conn.executescript(ADDED_TABLES)
    if not had_incidents:
        # Row 456: incidents open from events AFTER this migration, never from history, and the
        # first tick pass after a deploy still sees its own steps 1-4's events.
        with txn(conn):
            top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO guard_state (key, value, version, updated_at) VALUES (?,?,1,?)",
                         (INCIDENTS_WATERMARK, json.dumps({"event_id": top}), int(time.time())))


def migrations_pending(conn):
    """True if `migrate` would still add a column/table or widen `incidents.kind` (row 507(b)).
    Shared read-only oracle: the tick's startup migration and `capsule check`."""
    for table, col, _ in ADDED_COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            return True
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return (not {"incidents", "slack_msg", "outbox_key", "app_event_seen"} <= tables
            or bool(_incidents_missing(conn)) or _incidents_kind_needs_widening(conn))


def create_board(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.executescript(BOARD_DDL)
    migrate(conn)
    conn.close()


def copy_board(src, dst):
    """Copy a (stopped) old-system board with the SQLite backup API, then migrate the copy only."""
    dst = Path(dst)
    if dst.exists():
        raise FileExistsError(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    conn = connect(dst)
    try:
        migrate(conn)
    finally:
        conn.close()


def migrate_board(legacy_db, capsule_db):
    """A stopped handover's migration (row 420): `copy_board` the old-system file, apply
    Capsule's defaults for the columns the old system never wrote (NULL tier/risk_class
    -> 'strong', NULL file_set -> '[]', the same defaults T6 dispatch treats NULL
    as), and return a per-table row count for the handover record."""
    copy_board(legacy_db, capsule_db)
    conn = connect(capsule_db)
    try:
        with txn(conn):
            conn.execute("UPDATE tasks SET tier='strong' WHERE tier IS NULL")
            conn.execute("UPDATE tasks SET risk_class='strong' WHERE risk_class IS NULL")
            conn.execute("UPDATE tasks SET file_set='[]' WHERE file_set IS NULL")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()


def event(conn, task_id, kind, payload=None, *, run_pk=None, run_id=None, now=None):
    """Append one task event. run_id (the run's uuid) always lands in the payload (row 379 (44))."""
    if run_id is not None:
        payload = dict(payload or {}, run_id=run_id)
    # row 569: kind + payload keys are an allowlist (stderr; CI is hard)
    event_kinds.warn(kind, payload)
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
        (task_id, run_pk, kind, json.dumps(payload, ensure_ascii=False) if payload else None,
         int(now or time.time())))


CLEAR_CLAIM = "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL"
# Card state machine (row 509): target status -> statuses it may be entered from. `transition`
# is the only writer of tasks.status; the caller appends the event that says why.
TRANSITIONS = {
    "todo": {"ready", "running", "review", "blocked", "triage", "scheduled", "done"},
    "ready": {"todo", "running", "review", "blocked", "triage", "scheduled", "done"},
    "running": {"ready"},
    "review": {"running", "blocked", "triage"},
    "blocked": {"todo", "ready", "running", "review", "blocked", "triage", "scheduled", "done"},
    "triage": {"ready", "running", "review", "blocked", "triage", "done"},
    "done": {"todo", "ready", "running", "review", "blocked", "triage", "scheduled", "done"},
    "archived": {"todo", "ready", "blocked", "triage", "scheduled"},
}


def transition(conn, task_id, to, *, frm=None, claim=False, where="", params=(), **cols):
    """Move a card to `to`, set `cols`, and with `claim` clear the claim. `frm` (legal in
    TRANSITIONS) and `where`/`params` guard the move. True when the card moved."""
    if any(s not in TRANSITIONS[to] for s in frm or ()):
        raise ValueError(f"illegal card transition {frm} -> {to}")
    sets = "".join(f", {c}=?" for c in cols) + (f", {CLEAR_CLAIM}" if claim else "")
    guard = f" AND status IN ({','.join('?' * len(frm))})" if frm else ""
    return conn.execute(f"UPDATE tasks SET status=?{sets} WHERE id=?{guard}{where}",
                        (to, *cols.values(), task_id, *(frm or ()), *params)).rowcount == 1


def new_task(conn, *, title, body="", assignee=None, status="ready", branch=None, parents=(),
             risk_class="strong", file_set=(), request_needs=None, short_name=None, now=None,
             check_cmd=None, check_expect=None, size_tag=None, risk_flags=None, workload=None,
             tier=None):
    """`tier` is the runner tier; not given, it follows `risk_class` (today's behaviour)."""
    tid = "t_" + secrets.token_hex(4)
    now = int(now or time.time())
    with txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, created_at, created_by, branch_name,"
            " risk_class, tier, file_set, request_needs, short_name, name_source, check_cmd,"
            " check_expect, size_tag, risk_flags, workload, number)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            # per board; COUNT(*) keeps old NULL rows clear of the new numbers
            " (SELECT MAX(COALESCE(MAX(number), 0), COUNT(*)) + 1 FROM tasks))",
            (tid, title, body, assignee, status, now, "capsule", branch, risk_class,
             tier or risk_class, json.dumps(list(file_set)), request_needs, short_name,
             "human" if short_name else None, check_cmd, check_expect, size_tag,
             json.dumps(list(risk_flags)) if risk_flags else None, workload))
        for parent in parents:
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)", (parent, tid))
        event(conn, tid, "created", {"by": "capsule"}, now=now)
    return tid


def open_parent(conn, task_id):
    """Lowest id of a parent of `task_id` (`task_links`) that is not `done`/`archived`,
    else None - the one open-parent query `blocks`, `lease.claim` and `unblocked`
    share."""
    row = conn.execute(
        "SELECT p.id FROM task_links l JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') ORDER BY p.id LIMIT 1",
        (task_id,)).fetchone()
    return row[0] if row is not None else None


def guard_get(conn, key):
    row = conn.execute("SELECT value, version FROM guard_state WHERE key=?", (key,)).fetchone()
    return (None, 0) if row is None else (json.loads(row[0]), row[1])


def guard_set(conn, key, value, *, expect_version=None, now=None):
    """Compare-and-set on version (spec §6.1 guard_state; replaces last-writer-wins JSON files)."""
    now, blob = int(now or time.time()), json.dumps(value)
    with txn(conn):
        row = conn.execute("SELECT version FROM guard_state WHERE key=?", (key,)).fetchone()
        if row is None:
            if expect_version not in (None, 0):
                return False
            conn.execute("INSERT INTO guard_state (key, value, version, updated_at) VALUES (?,?,1,?)",
                         (key, blob, now))
            return True
        if expect_version is not None and row[0] != expect_version:
            return False
        conn.execute("UPDATE guard_state SET value=?, version=version+1, updated_at=? WHERE key=? AND version=?",
                     (blob, now, key, row[0]))
    return True
