import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from capsule import db
from capsule.tests.legacy_oracle import legacy as legacy_oracle, needs_legacy as needs_legacy
from capsule.tests.fixtures.board_ddl import board_ddl

ADDED_TABLES = {"outbox", "guard_state", "thread_owner", "requests"}
LEGACY_TABLES = {"tasks", "task_links", "task_comments", "task_events",
                 "task_runs", "task_attachments", "kanban_notify_subs"}


def legacy_only_board(path):
    conn = sqlite3.connect(path)
    conn.executescript(board_ddl())
    conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_0000aaaa', 'x', 'ready', 1)")
    conn.commit()
    conn.close()


def sql_shape(conn, kind="table"):
    return [tuple(r) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type=? ORDER BY name", (kind,))]


def columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


class BoardDdlName(unittest.TestCase):
    def test_board_schema_file_and_ddl_create_tasks(self):
        self.assertTrue((Path(db.__file__).with_name("board_schema.sql")).is_file())
        conn = sqlite3.connect(":memory:")
        conn.executescript(board_ddl())
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("tasks", names)


class BoardSchema(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_create_board_has_legacy_and_added_tables(self):
        p = self.dir / "b" / "kanban.db"
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(LEGACY_TABLES | ADDED_TABLES <= names)
        self.assertTrue({"failure_count", "failure_reset_at", "risk_class", "tier", "file_set",
                         "contract_ref", "request_needs"} <= columns(conn, "tasks"))
        self.assertTrue({"run_id", "provider", "account", "model", "runner", "tokens_in", "tokens_out",
                         "tokens_cache_read", "handoff", "resumed_from", "attempts", "error_class"}
                        <= columns(conn, "task_runs"))
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_migrate_is_idempotent(self):
        p = self.dir / "kanban.db"
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        before = columns(conn, "tasks")
        db.migrate(conn)
        db.migrate(conn)
        self.assertEqual(columns(conn, "tasks"), before)

    def test_copy_board_migrates_the_copy_and_leaves_the_source(self):
        src, dst = self.dir / "src.db", self.dir / "cap" / "kanban.db"
        legacy_only_board(src)
        db.copy_board(src, dst)
        s = sqlite3.connect(src)
        self.addCleanup(s.close)
        self.assertNotIn("failure_count", columns(s, "tasks"))
        conn = db.connect(dst)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT id, failure_count FROM tasks").fetchone()
        self.assertEqual((row["id"], row["failure_count"]), ("t_0000aaaa", 0))

    def test_copy_board_refuses_an_existing_destination(self):
        src, dst = self.dir / "src.db", self.dir / "dst.db"
        legacy_only_board(src)
        dst.write_text("")
        with self.assertRaises(FileExistsError):
            db.copy_board(src, dst)

    def test_nested_txn_rolls_back_only_the_inner_part(self):
        p = self.dir / "kanban.db"
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        with db.txn(conn):
            conn.execute("INSERT INTO guard_state (key, value, updated_at) VALUES ('a', '1', 1)")
            with self.assertRaises(RuntimeError):
                with db.txn(conn):
                    conn.execute("INSERT INTO guard_state (key, value, updated_at) VALUES ('b', '1', 1)")
                    raise RuntimeError("inner")
        keys = [r[0] for r in conn.execute("SELECT key FROM guard_state WHERE key != ?", (db.INCIDENTS_WATERMARK,))]
        self.assertEqual(keys, ["a"])

    def test_guard_set_is_compare_and_set(self):
        p = self.dir / "kanban.db"
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        self.assertTrue(db.guard_set(conn, "owner", {"owner": "capsule"}, now=5))
        value, version = db.guard_get(conn, "owner")
        self.assertEqual((value, version), ({"owner": "capsule"}, 1))
        self.assertFalse(db.guard_set(conn, "owner", {"owner": "legacy"}, expect_version=7))
        self.assertTrue(db.guard_set(conn, "owner", {"owner": "legacy"}, expect_version=1))
        self.assertEqual(db.guard_get(conn, "owner"), ({"owner": "legacy"}, 2))
        self.assertEqual(db.guard_get(conn, "missing"), (None, 0))

    def test_event_carries_run_id_in_payload(self):
        p = self.dir / "kanban.db"
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        tid = db.new_task(conn, title="t", branch="epic/x", now=3)
        db.event(conn, tid, "claimed", {"a": 1}, run_pk=9, run_id="ab" * 16, now=4)
        row = conn.execute("SELECT run_id, payload FROM task_events WHERE kind='claimed'").fetchone()
        self.assertEqual(row["run_id"], 9)
        self.assertEqual(json.loads(row["payload"]), {"a": 1, "run_id": "ab" * 16})


CARD_COLUMNS = {"check_cmd", "check_expect", "size_tag", "risk_flags", "passes", "workload"}


class CardFields(unittest.TestCase):
    """R04 intent rows null-fields-are-legacy, depends-uses-task-links. Oracle 1: the sqlite column
    read. Oracle 2: the bundle a role would read (and, in test_human_author, the CLI echo)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_null_fields_are_legacy(self):
        from capsule import bundles
        src, dst = self.dir / "src.db", self.dir / "cap" / "kanban.db"
        legacy_only_board(src)
        db.copy_board(src, dst)                     # an old board gains the six columns, NULL
        conn = db.connect(dst)
        self.addCleanup(conn.close)
        self.assertTrue(CARD_COLUMNS <= columns(conn, "tasks"))
        tid = db.new_task(conn, title="legacy")
        for t in ("t_0000aaaa", tid):
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (t,)).fetchone()
            self.assertEqual([row[c] for c in sorted(CARD_COLUMNS)], [None] * 6)
        risk = conn.execute("SELECT risk_class FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        self.assertEqual(risk, "strong")
        card = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        self.assertNotIn("Card fields", bundles.implementer(card)["prompt"])
        self.assertNotIn("Card fields", bundles.reviewer(card, "")["prompt"])

    def test_migration_adds_the_columns_once_and_keeps_every_card(self):
        src, dst = self.dir / "src.db", self.dir / "cap" / "kanban.db"
        legacy_only_board(src)
        db.copy_board(src, dst)
        conn = db.connect(dst)
        self.addCleanup(conn.close)
        shape = sql_shape(conn)
        db.migrate(conn)
        self.assertEqual(sql_shape(conn), shape)
        self.assertFalse(db.migrations_pending(conn))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_a_card_round_trips_every_field(self):
        p = self.dir / "kanban.db"
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        tid = db.new_task(conn, title="t", check_cmd="python3 -m unittest x", check_expect="OK",
                          size_tag="m", risk_flags=["data", "judges"], workload="product")
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        got = [row[c] for c in ("check_cmd", "check_expect", "size_tag", "workload", "passes")]
        self.assertEqual(got, ["python3 -m unittest x", "OK", "m", "product", None])
        self.assertEqual(json.loads(row["risk_flags"]), ["data", "judges"])
        self.assertEqual(row["risk_class"], "strong")


class LegacyOracle(unittest.TestCase):
    @needs_legacy
    def test_ddl_is_legacy_verbatim(self):
        home = Path(tempfile.mkdtemp())
        theirs = legacy_oracle("print(json.dumps(kb.SCHEMA_SQL))", home)
        self.assertIn(theirs.strip(), board_ddl())

    @needs_legacy
    def test_legacy_opens_and_claims_on_a_migrated_copy(self):
        home = Path(tempfile.mkdtemp())
        src, dst = home / "src.db", home / "dst.db"
        legacy_only_board(src)
        db.copy_board(src, dst)
        got = legacy_oracle(
            f"from pathlib import Path\n"
            f"conn = kb.connect(Path({str(dst)!r}))\n"
            f"t = kb.claim_task(conn, 't_0000aaaa', claimer='h:1')\n"
            f"cols = [r[1] for r in conn.execute('PRAGMA table_info(tasks)')]\n"
            f"print(json.dumps({{'claimed': t is not None, 'has': 'failure_count' in cols}}))", home)
        self.assertEqual(got, {"claimed": True, "has": True})
        conn = db.connect(dst)
        self.addCleanup(conn.close)
        self.assertIn("outbox", {r[0] for r in conn.execute("SELECT name FROM sqlite_master")})


class IncidentsTable(unittest.TestCase):
    """Row 456: the incidents table is additive; migrating a board with data changes no row."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def dump(self, conn):
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                                    " AND name != 'incidents' ORDER BY name"):
            out[name] = [tuple(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
        return out

    def test_migrate_adds_an_empty_incidents_table_and_leaves_every_other_row(self):
        p = self.dir / "kanban.db"
        conn = sqlite3.connect(p)
        conn.executescript(board_ddl())
        conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_0000aaaa', 'x', 'ready', 1)")
        conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t_0000aaaa', 'created', 1)")
        conn.commit()
        conn.close()
        raw = sqlite3.connect(p)
        before = self.dump(raw)            # a real pre-migration board (legacy tables only)
        raw.close()
        self.assertEqual(set(before), LEGACY_TABLES)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        db.migrate(conn)
        after = self.dump(conn)
        self.assertEqual({t: after[t] for t in LEGACY_TABLES if t != "tasks"},
                         {t: before[t] for t in LEGACY_TABLES if t != "tasks"})
        # tasks gains Capsule's additive columns: the legacy columns of every row are unchanged
        width = len(before["tasks"][0])
        self.assertEqual([r[:width] for r in after["tasks"]], before["tasks"])
        db.migrate(conn)                   # a second pass is a no-op
        self.assertEqual(self.dump(conn), after)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
        self.assertTrue({"id", "board", "task_id", "run_id", "kind", "opened_at", "evidence", "root_cause", "lesson",
                         "mitigation", "enforcer", "recurrence_test", "decision_row", "fingerprint", "status",
                         "closed_at"} <= columns(conn, "incidents"))

    def test_kind_and_status_are_checked_by_the_database(self):
        p = self.dir / "kanban.db"
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        row = "INSERT INTO incidents (id, board, kind, opened_at, fingerprint, status) VALUES (?,?,?,?,?,?)"
        conn.execute(row, ("inc_00000001", "b", "crash", 1, "f", "open"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(row, ("inc_00000002", "b", "meltdown", 1, "f", "open"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(row, ("inc_00000003", "b", "crash", 1, "f", "resolved"))


# The pre-507(b) `incidents` DDL (no `review_infra` in the `kind` CHECK): a live board that ran
# `migrate` before this branch has exactly this table on disk. Frozen verbatim (never derived from
# `db._incidents_body()`, which already carries `review_infra`) so the widening test is a real
# oracle against the OLD schema, not a tautology against the code under test.
OLD_INCIDENTS_DDL = """
CREATE TABLE incidents (
  id TEXT PRIMARY KEY, board TEXT NOT NULL, task_id TEXT, run_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('crash','hang','silent_failure','contradiction',
    'human_intervention','override','escaped_bug','repeat')),
  opened_at INTEGER NOT NULL, evidence TEXT NOT NULL DEFAULT '[]', root_key TEXT,
  root_cause TEXT, lesson TEXT, mitigation TEXT, enforcer TEXT, recurrence_test TEXT,
  decision_row INTEGER, decision_row_text TEXT, confidence TEXT, fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','postmortem','fix_pending','closed')),
  closed_at INTEGER, repeat_of TEXT, fix_card TEXT, korin_task TEXT, note_path TEXT, role TEXT,
  main_sha TEXT);
CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents(fingerprint, opened_at);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
"""


class IncidentsKindWidening(unittest.TestCase):
    """Row 507(b) fix (card t_67a95702), concern 2: a live board's `incidents` table predates
    `review_infra`/`invalid_document` in the `kind` CHECK (SQLite has no `ALTER TABLE ... CHECK`,
    so the old boards on disk keep the old constraint until this migration rebuilds the table in
    place). Two oracles per check: the DB's own CHECK enforcement (an insert either raises or does
    not) and the row data surviving the rebuild untouched."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def old_board(self):
        """A board with every OTHER migration already applied (all of `db.ADDED_COLUMNS`, every
        other `db.ADDED_TABLES` table) - only `incidents.kind` still predates `review_infra`/
        `invalid_document`. Fix round 2: the old fixture built straight from `BOARD_DDL` with none
        of `ADDED_COLUMNS`, so `migrations_pending` was True regardless of the widening check
        (`_incidents_kind_needs_widening` was never actually consulted) - vacuous against a hand
        mutant that dropped `or _incidents_kind_needs_widening(conn)`."""
        p = self.dir / "kanban.db"
        conn = sqlite3.connect(p)
        conn.executescript(board_ddl())
        for table, col, decl in db.ADDED_COLUMNS:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,"
            " thread_key TEXT, body TEXT NOT NULL, created_at INTEGER NOT NULL, attempts INTEGER NOT NULL"
            " DEFAULT 0, delivered_at INTEGER, last_error TEXT, status TEXT NOT NULL DEFAULT 'pending');"
            "CREATE TABLE IF NOT EXISTS guard_state (key TEXT PRIMARY KEY, value TEXT NOT NULL,"
            " version INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS thread_owner (channel TEXT NOT NULL, thread_ts TEXT NOT NULL,"
            " task_id TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY (channel, thread_ts));"
            "CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,"
            " claim_lock TEXT, verb TEXT, schema TEXT, body TEXT NOT NULL, status TEXT NOT NULL, error TEXT,"
            " created_at INTEGER NOT NULL);"
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_run_id ON task_runs(run_id);")
        conn.executescript(OLD_INCIDENTS_DDL)
        conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_0000aaaa', 'x', 'ready', 1)")
        conn.execute(
            "INSERT INTO incidents (id, board, kind, opened_at, fingerprint, status, evidence)"
            " VALUES ('inc_00000001', 'b', 'crash', 1, 'f', 'open', '[]')")
        conn.commit()
        conn.close()
        return p

    def test_an_old_schema_db_migrates_and_then_accepts_a_review_infra_insert(self):
        p = self.old_board()
        raw = sqlite3.connect(p)
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint) VALUES"
                       " ('inc_00000002', 'b', 'review_infra', 2, 'f2')")
        raw.close()
        conn = db.connect(p)
        self.addCleanup(conn.close)
        db.migrate(conn)
        # oracle 1: the CHECK itself now accepts review_infra and invalid_document
        conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint) VALUES"
                     " ('inc_00000002', 'b', 'review_infra', 2, 'f2')")
        conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint) VALUES"
                     " ('inc_00000004', 'b', 'invalid_document', 4, 'f4')")
        self.assertEqual(conn.execute("SELECT kind FROM incidents WHERE id='inc_00000002'").fetchone()[0],
                         "review_infra")
        self.assertEqual(conn.execute("SELECT kind FROM incidents WHERE id='inc_00000004'").fetchone()[0],
                         "invalid_document")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint) VALUES"
                         " ('inc_00000003', 'b', 'meltdown', 3, 'f3')")
        # oracle 2: the pre-migration row survived the rebuild untouched
        row = conn.execute("SELECT id, board, kind, opened_at, fingerprint, status, evidence FROM incidents"
                           " WHERE id='inc_00000001'").fetchone()
        self.assertEqual(tuple(row), ("inc_00000001", "b", "crash", 1, "f", "open", "[]"))
        self.assertEqual(conn.execute("SELECT title FROM tasks WHERE id='t_0000aaaa'").fetchone()[0], "x")

    def test_migrating_an_already_widened_board_twice_is_a_no_op(self):
        p = self.old_board()
        conn = db.connect(p)
        self.addCleanup(conn.close)
        db.migrate(conn)
        sql_once = conn.execute("SELECT sql FROM sqlite_master WHERE name='incidents'").fetchone()[0]
        rows_once = conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()
        db.migrate(conn)
        db.migrate(conn)
        sql_twice = conn.execute("SELECT sql FROM sqlite_master WHERE name='incidents'").fetchone()[0]
        rows_twice = conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()
        self.assertEqual(sql_once, sql_twice)
        self.assertEqual([tuple(r) for r in rows_once], [tuple(r) for r in rows_twice])

    def test_a_fresh_board_already_has_the_widened_check(self):
        p = self.dir / "fresh.db"
        db.create_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint) VALUES"
                     " ('inc_00000001', 'b', 'review_infra', 1, 'f')")
        conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint) VALUES"
                     " ('inc_00000002', 'b', 'invalid_document', 2, 'f2')")
        self.assertEqual([r[0] for r in conn.execute("SELECT kind FROM incidents ORDER BY id")],
                         ["review_infra", "invalid_document"])
        self.assertFalse(db._incidents_kind_needs_widening(conn))

    def test_migrations_pending_is_honest_about_an_un_widened_check(self):
        """F6: `capsule check`/`preflight.py` reads `db.migrations_pending` on a read-only
        connection; it must not report "nothing pending" while an insert of the new kind would
        still raise."""
        p = self.old_board()
        ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        self.addCleanup(ro.close)
        self.assertTrue(db.migrations_pending(ro))
        conn = db.connect(p)
        self.addCleanup(conn.close)
        db.migrate(conn)
        ro2 = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ro2.row_factory = sqlite3.Row
        self.addCleanup(ro2.close)
        self.assertFalse(db.migrations_pending(ro2))


class ShortNames(unittest.TestCase):
    def board(self, name):
        d = Path(tempfile.mkdtemp()) / name
        db.create_board(d)
        conn = db.connect(d)
        self.addCleanup(conn.close)
        return conn

    def test_migrate_adds_name_columns_once_to_an_old_board(self):
        p = Path(tempfile.mkdtemp()) / "old.db"
        legacy_only_board(p)
        conn = db.connect(p)
        self.addCleanup(conn.close)
        db.migrate(conn)
        db.migrate(conn)
        self.assertTrue({"short_name", "number", "name_source"} <= columns(conn, "tasks"))
        self.assertIsNone(conn.execute("SELECT number FROM tasks").fetchone()[0])

    def test_numbers_count_up_per_board_and_boards_are_independent(self):
        a, b = self.board("a.db"), self.board("b.db")
        got_a = [db.new_task(a, title=f"a{i}") for i in range(3)]
        got_b = db.new_task(b, title="b0")
        sql = "SELECT number FROM tasks WHERE id=?"
        self.assertEqual([a.execute(sql, (t,)).fetchone()[0] for t in got_a], [1, 2, 3])
        self.assertEqual(b.execute(sql, (got_b,)).fetchone()[0], 1)

    def test_unnumbered_old_rows_are_counted_so_backfill_cannot_collide(self):
        a = self.board("a.db")
        for i in range(3):
            a.execute("INSERT INTO tasks (id, title, status, created_at)"
                      " VALUES (?, 'old', 'done', 1)", (f"t_0000000{i}",))
        a.commit()
        sql = "SELECT number FROM tasks WHERE id=?"
        first, second = db.new_task(a, title="n1"), db.new_task(a, title="n2")
        self.assertEqual([a.execute(sql, (t,)).fetchone()[0] for t in (first, second)], [4, 5])

    def test_short_name_is_stored_as_human_sourced(self):
        a = self.board("a.db")
        t = db.new_task(a, title="x", short_name="Nice name")
        u = db.new_task(a, title="y")
        sql = "SELECT short_name, name_source FROM tasks WHERE id=?"
        self.assertEqual(tuple(a.execute(sql, (t,)).fetchone()), ("Nice name", "human"))
        self.assertEqual(tuple(a.execute(sql, (u,)).fetchone()), (None, None))


class TransitionIntent(unittest.TestCase):
    """Z1: the `verb: transition` examples in `contracts/intent/interventions.jsonl`, each run
    through the real `db.transition`."""

    def test_every_example_matches_what_db_transition_does(self):
        from capsule import intent
        examples = [e for e in intent.load("interventions")
                    if e["given"]["document"]["verb"] == "transition"]
        self.assertEqual(len(examples), 2)
        for ex in examples:
            doc = ex["given"]["document"]
            path = Path(tempfile.mkdtemp()) / "b.db"
            db.create_board(path)
            conn = db.connect(path)
            self.addCleanup(conn.close)
            tid = db.new_task(conn, title="t", status=doc["status"])
            try:
                applied = db.transition(conn, tid, doc["to"], frm=tuple(doc["frm"]))
            except ValueError:
                applied = False
            self.assertEqual(applied, ex["expect"]["applied"], ex["id"])
            self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone()[0],
                             ex["expect"]["status"], ex["id"])


class HotPathIndexes(unittest.TestCase):
    """followups 380/401: per-tick scans by tasks.branch_name and task_events.run_id are indexed;
    the indexes are additive (IF NOT EXISTS), so migrating an old board creates them."""

    WANT = {"idx_tasks_branch_name": ("tasks", "branch_name"),
            "idx_task_events_run_id": ("task_events", "run_id")}

    def check(self, conn):
        for name, (table, col) in self.WANT.items():
            rows = list(conn.execute(f"PRAGMA index_info({name})"))
            self.assertEqual([r[2] for r in rows], [col], name)
            on = conn.execute("SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                              (name,)).fetchone()
            self.assertEqual(on[0], table)

    def test_fresh_board_has_the_indexes(self):
        path = Path(tempfile.mkdtemp()) / "k.db"
        db.create_board(path)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        self.check(conn)

    def test_migrating_an_old_board_adds_them_and_twice_is_a_no_op(self):
        path = Path(tempfile.mkdtemp()) / "k.db"
        legacy_only_board(path)
        conn = db.connect(path)
        self.addCleanup(conn.close)
        db.migrate(conn)
        db.migrate(conn)
        self.check(conn)
