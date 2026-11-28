import datetime
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[n]) for n in (1, 2)]
import capsule.tests  # noqa: E402,F401 - its __init__ installs the swept TMPDIR (tmpclean)
os.environ["AGENT_OPS_NO_SEND"] = "1"  # tests must never send Telegram / reach the real db
os.environ.setdefault("EVENTS_SPOOL", str(Path(tempfile.mkdtemp(prefix="events-obs-")) / "events.jsonl"))
import events
import obs_probe

_ENV_PATCH = unittest.mock.patch.dict(os.environ)


def setUpModule():
    """Boards resolve under each test's temp HOME: another module's import-time
    CAPSULE_HOME (discover imports every module first) must not leak in."""
    _ENV_PATCH.start()
    os.environ.pop("CAPSULE_HOME", None)


def tearDownModule():
    _ENV_PATCH.stop()

NOW = datetime.datetime(2026, 9, 8, 12, 0, 0, tzinfo=datetime.timezone.utc)


class FakeRun:
    """Routes an argv onto a canned (returncode, stdout, stderr) or an
    Exception to raise, so a collector's real subprocess call never happens.
    Records every call for argv-shape / stdin assertions.

    `_key` requires argv[0] == "docker" for the psql/stats/inspect routes -
    on purpose: a regression back to `events.docker_bin()` (a directory, not
    an executable - review Critical) as argv[0] falls through to the
    catch-all `tuple(argv)` key, which has no canned response, so FakeRun
    raises instead of silently answering. That is what makes
    `Postgres.test_argv_shape_and_env` (and `Ship.test_argv_shape_and_stdin`)
    actually catch the bug rather than agreeing with it."""

    def __init__(self, responses=None, **kwargs):
        self.responses = dict(responses or {})
        self.responses.update(kwargs)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        key = self._key(argv)
        resp = self.responses.get(key)
        if resp is None:
            raise AssertionError(f"FakeRun: no canned response for {key!r} (argv={argv})")
        if isinstance(resp, Exception):
            raise resp
        rc, out, err = resp
        return subprocess.CompletedProcess(argv, rc, out, err)

    @staticmethod
    def _key(argv):
        if argv and argv[0] == "docker" and "psql" in argv:
            if any("COPY samples" in a for a in argv if isinstance(a, str)):
                return "ship"
            if any("INTO samples_hourly" in a for a in argv if isinstance(a, str)):
                return "rollup"
            if any("'rulings'" in a for a in argv if isinstance(a, str)):
                return "power"
            return "postgres"
        if argv and argv[0] == "docker" and len(argv) > 1 and argv[1] == "stats":
            return "docker_stats"
        if argv and argv[0] == "docker" and len(argv) > 1 and argv[1] == "ps":
            return "docker_ps"
        if argv and argv[0] == "docker" and len(argv) > 1 and argv[1] == "inspect":
            return ("docker_inspect", argv[-1])   # one canned answer per container (docker/sandbox share this)
        if argv and argv[0] == "launchctl":
            return "launchd"
        if argv and argv[0] == "df":
            return "df"
        if argv and argv[0] == "sysctl":
            return "sysctl"
        return tuple(argv)


POSTGRES_CSV = (
    "connections,active,2\n"
    "connections,idle,1\n"
    "db.bytes,,123456\n"
    "table.rows,events,10\n"
    "table.rows,samples,5\n"
    "table.bytes,events,2048\n"
    "table.bytes,samples,1024\n"
    "events.per_hour,,7\n"
    "wal.bytes,,33554432\n"
)

DOCKER_STATS = (
    '{"Name":"capsule-corp-postgres","CPUPerc":"1.23%","MemUsage":"123.4MiB / 7.6GiB"}\n'
    '{"Name":"capsule-corp-grafana","CPUPerc":"0.50%","MemUsage":"50.0MiB / 7.6GiB"}\n'
    '{"Name":"some-other-container","CPUPerc":"9.99%","MemUsage":"1.0GiB / 7.6GiB"}\n'
)

# One inspect line per container - `docker inspect -f ... <name>` is now
# called once per name (review Important 1), so each needs its own canned
# reply keyed by container name.
DOCKER_INSPECT_LINES = {
    "capsule-corp-postgres":       "/capsule-corp-postgres 0 running\n",
    "capsule-corp-loki":           "/capsule-corp-loki 1 running\n",
    "capsule-corp-fluent-bit":     "/capsule-corp-fluent-bit 0 exited\n",
    "capsule-corp-grafana":        "/capsule-corp-grafana 0 running\n",
    "capsule-corp-infisical":      "/capsule-corp-infisical 0 running\n",
    "capsule-corp-infisical-db":   "/capsule-corp-infisical-db 0 running\n",
    "capsule-corp-infisical-redis": "/capsule-corp-infisical-redis 0 running\n",
}

# `docker ps --filter label=capsule-run-id` names for the sandbox collector: docker does the
# filtering, so only worker containers are listed, whatever image they run.
DOCKER_PS_OUT = "cap-dev-9f3a\ncap-qa-1b2c\n"
SANDBOX_INSPECT_LINES = {
    "cap-dev-9f3a": "none \n",
    "cap-qa-1b2c": "bridge \n",
}

LAUNCHCTL_LIST = (
    "PID\tStatus\tLabel\n"
    "1234\t0\tdev.capsule.capsule-corp-heal\n"
    "-\t0\tdev.capsule.morning-brief\n"
    "5678\t0\tcom.apple.somethingelse\n"
)

DF_OUT = (
    "Filesystem   1024-blocks      Used Available Capacity iused     ifree %iused  Mounted on\n"
    "/dev/disk3s5   482797652 346191796  97175636    79% 3422199 971756360    0%   /System/Volumes/Data\n"
)

SYSCTL_OUT = "{ 2.79 2.60 2.37 }\n"


def full_fake_run(inspect_overrides=None, **overrides):
    """`inspect_overrides`: {container: (rc, out, err)} to replace one or more
    of the seven per-container `docker inspect` canned replies (e.g. to model
    an absent container) without touching the rest."""
    resp = {
        "postgres": (0, POSTGRES_CSV, ""),
        "docker_stats": (0, DOCKER_STATS, ""),
        "docker_ps": (0, DOCKER_PS_OUT, ""),
        "launchd": (0, LAUNCHCTL_LIST, ""),
        "df": (0, DF_OUT, ""),
        "sysctl": (0, SYSCTL_OUT, ""),
        "ship": (0, "", ""),
        "rollup": (0, "", ""),
        "power": (0, "rulings,,68\n"
                     "decisions,,10\n"
                     "retrieval,capsule-corp/t_63a8b207,1600\n", ""),
    }
    for name, line in DOCKER_INSPECT_LINES.items():
        resp[("docker_inspect", name)] = (0, line, "")
    for name, line in SANDBOX_INSPECT_LINES.items():
        resp[("docker_inspect", name)] = (0, line, "")
    for name, r in (inspect_overrides or {}).items():
        resp[("docker_inspect", name)] = r
    resp.update(overrides)
    return FakeRun(resp)


_OPEN_WRITE_CONS = []  # keeps WAL-mode writer connections alive for the life of the test process -
                        # SQLite auto-checkpoints (and deletes -wal) when the LAST connection to a
                        # database closes, so a fixture that wants real un-checkpointed WAL frames on
                        # disk must never let this connection close (module test process exit reaps it)


def make_board_db(dir_path, board="capsule-corp", checkpoint=False):
    """A real WAL-mode kanban.db fixture, not a fake -wal file glued on: the
    review flagged that a fixture with journal_mode left at its rollback-
    journal default plus a hand-written -wal file never actually exercises
    reading committed rows back out of WAL frames (a live kanban board is
    always WAL-mode, and always has a writer connection open - the old store never
    closes its db handle between ticks, which is exactly what keeps a real
    board's -wal file populated). `checkpoint=True` instead folds the WAL
    back into the main file and truncates -wal to 0 bytes (PRAGMA
    wal_checkpoint(TRUNCATE)) then closes normally, covering the "no -wal
    present" shape - both must read back identically through collect_boards."""
    board_dir = Path(dir_path) / board
    board_dir.mkdir(parents=True, exist_ok=True)
    db_path = board_dir / "kanban.db"
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            profile TEXT,
            status TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            outcome TEXT,
            metadata TEXT,
            error TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    now_epoch = int(NOW.timestamp())
    con.executemany("INSERT INTO tasks (id, status, consecutive_failures) VALUES (?, ?, ?)", [
        ("t1", "ready", 0),
        ("t2", "ready", 0),
        ("t3", "done", 0),
        ("t4", "done", 0),
        ("t5", "done", 0),
        ("t6", "blocked", 2),
    ])
    con.executemany(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)", [
            ("t1", "done", now_epoch - 3600, now_epoch - 3590, "completed"),      # duration 10
            ("t2", "done", now_epoch - 3000, now_epoch - 2980, "completed"),      # duration 20
            ("t3", "done", now_epoch - 2000, now_epoch - 1970, "completed"),      # duration 30
            ("t4", "running", now_epoch - 100, None, None),                       # still running
            ("t5", "done", now_epoch - 200000, now_epoch - 199990, "completed"),  # > 24h old, excluded
        ])
    con.commit()
    if checkpoint:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    else:
        _OPEN_WRITE_CONS.append(con)  # deliberately not closed - see the module-level comment above
    return db_path


def add_card_rows(db_path, now_epoch):
    """Three runs on t1 and two identical failures on t6 - the three-strike shape."""
    con = sqlite3.connect(str(db_path))
    con.executemany(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at,"
        " outcome, metadata, error) VALUES (?,?,?,?,?,?,?,?)", [
            ("t1", "dev", "done", now_epoch - 900, now_epoch - 800, "completed",
             json.dumps({"worker_session_id": "s-dev-1"}), None),
            ("t1", "qa", "done", now_epoch - 700, now_epoch - 690, "completed",
             json.dumps({"worker_session_id": "s-qa-1"}), None),
            ("t1", "dev", "crashed", now_epoch - 600, now_epoch - 590, "crashed",
             None, "worker pid 4711 died reading /tmp/ab39f01c2d/x"),
            ("t6", "dev", "crashed", now_epoch - 500, now_epoch - 490, "crashed",
             json.dumps({"worker_session_id": "s-missing"}),
             "worker pid 12 died reading /tmp/ff00aa11bb/y"),
            ("t6", "dev", "crashed", now_epoch - 400, now_epoch - 390, "crashed",
             json.dumps({"worker_session_id": "s-missing"}),
             "worker pid 98765 died reading /tmp/0011223344/y"),
        ])
    con.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
        " VALUES (?,?,?,?,?)", [
            ("t1", None, "created", None, now_epoch - 1000),
            ("t1", None, "changes_requested", None, now_epoch - 850),
            ("t1", None, "changes_requested", None, now_epoch - 750),
        ])
    con.commit()
    con.close()


class Names(unittest.TestCase):
    def test_every_name_maps_to_a_known_collector(self):
        for name, meta in obs_probe.NAMES.items():
            self.assertIn(meta["collector"], obs_probe.COLLECTOR_NAMES)
            self.assertIsInstance(meta["labels"], tuple)
            self.assertTrue(meta["unit"])


class Postgres(unittest.TestCase):
    def test_collect_postgres_exact_samples(self):
        run = full_fake_run()
        samples = obs_probe.collect_postgres(run, now=NOW)
        got = {(s.name, tuple(sorted(s.labels.items()))): s.value for s in samples}
        self.assertEqual(got[("capsule.postgres.connections", (("state", "active"),))], 2.0)
        self.assertEqual(got[("capsule.postgres.connections", (("state", "idle"),))], 1.0)
        self.assertEqual(got[("capsule.postgres.db.bytes", ())], 123456.0)
        self.assertEqual(got[("capsule.postgres.table.rows", (("table", "events"),))], 10.0)
        self.assertEqual(got[("capsule.postgres.table.rows", (("table", "samples"),))], 5.0)
        self.assertEqual(got[("capsule.postgres.table.bytes", (("table", "events"),))], 2048.0)
        self.assertEqual(got[("capsule.postgres.table.bytes", (("table", "samples"),))], 1024.0)
        self.assertEqual(got[("capsule.postgres.events.per_hour", ())], 7.0)
        self.assertEqual(got[("capsule.postgres.wal.bytes", ())], 33554432.0)
        for s in samples:
            self.assertIn(s.name, obs_probe.NAMES)
            self.assertEqual(s.service, "capsule")

    def test_null_connection_state_becomes_unknown(self):
        run = FakeRun(postgres=(0, "connections,,4\n", ""))
        samples = obs_probe.collect_postgres(run, now=NOW)
        self.assertEqual([(s.name, s.labels, s.value) for s in samples],
                          [("capsule.postgres.connections", {"state": "unknown"}, 4.0)])

    def test_psql_failure_raises(self):
        run = full_fake_run(postgres=(1, "", "ERROR: connection refused"))
        with self.assertRaises(Exception):
            obs_probe.collect_postgres(run, now=NOW)

    def test_argv_shape_and_env(self):
        """Mirrors Ship.test_argv_shape_and_stdin: argv[0] must be the literal
        "docker" (PATH-resolved via events.docker_env()), never
        events.docker_bin() - a directory, not an executable, whose use here
        raised a real EACCES against the live capsule-corp-postgres container
        and silently dropped every postgres sample every tick (review
        Critical)."""
        run = full_fake_run()
        obs_probe.collect_postgres(run, now=NOW)
        self.assertEqual(len(run.calls), 1)
        argv, kwargs = run.calls[0]
        self.assertEqual(argv[0], "docker")
        self.assertEqual(argv[1:5], ["exec", "-i", "capsule-corp-postgres", "psql"])
        self.assertIn("-U", argv); self.assertIn("ops", argv)
        self.assertIn("-d", argv); self.assertIn("ops", argv)
        self.assertIn("-c", argv)
        self.assertEqual(kwargs.get("env"), events.docker_env(guard=False))


class Boards(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="obs-home-"))
        self.boards_root = self.home / ".local/share/capsule/boards"
        self.db_path = make_board_db(self.boards_root)

    def _assert_common_samples(self, samples, board="capsule-corp"):
        got = {(s.name, tuple(sorted(s.labels.items()))): s.value for s in samples}
        self.assertEqual(got[("capsule.board.cards", (("board", board), ("status", "ready")))], 2.0)
        self.assertEqual(got[("capsule.board.cards", (("board", board), ("status", "done")))], 3.0)
        self.assertEqual(got[("capsule.board.cards", (("board", board), ("status", "blocked")))], 1.0)
        self.assertEqual(got[("capsule.board.runs.24h", (("board", board), ("outcome", "completed")))], 3.0)
        self.assertEqual(got[("capsule.board.runs.24h", (("board", board), ("outcome", "running")))], 1.0)
        self.assertEqual(got[("capsule.board.run.duration_s.p50", (("board", board),))], 20.0)
        self.assertEqual(got[("capsule.board.run.duration_s.p95", (("board", board),))], 30.0)
        self.assertEqual(got[("capsule.board.failures.open", (("board", board),))], 1.0)
        for s in samples:
            self.assertIn(s.name, obs_probe.NAMES)
        return got

    def test_collect_boards_exact_samples_with_wal_present_and_db_untouched(self):
        """The fixture is a real WAL-mode db with an open writer connection
        (see make_board_db), so its -wal file genuinely holds committed frames
        that have not been folded into the main file yet - this is what a
        live kanban board looks like, and what collect_boards must read
        correctly (review Important 2: the previous fixture's -wal file was
        32 fake zero bytes that were never actually read as WAL)."""
        wal_path = self.db_path.parent / "kanban.db-wal"
        self.assertTrue(wal_path.exists() and wal_path.stat().st_size > 0,
                         "fixture bug: expected real un-checkpointed WAL frames on disk")
        before = self.db_path.read_bytes()
        wal_before = wal_path.read_bytes()
        samples = obs_probe.collect_boards(self.home, now=NOW)
        after = self.db_path.read_bytes()
        wal_after = wal_path.read_bytes()
        self.assertEqual(before, after, "read-only collection must not mutate the fixture db")
        self.assertEqual(wal_before, wal_after, "read-only collection must not mutate/checkpoint -wal")

        got = self._assert_common_samples(samples)
        self.assertEqual(got[("capsule.board.db.bytes", (("board", "capsule-corp"),))], os.path.getsize(self.db_path))
        self.assertEqual(got[("capsule.board.wal.bytes", (("board", "capsule-corp"),))], wal_path.stat().st_size)

    def test_collect_boards_exact_samples_after_checkpoint_removes_wal(self):
        """Same fixture, but checkpointed+truncated first - the -wal file is
        gone (or empty), and the data now lives entirely in the main file.
        Both shapes must read back identically."""
        home = Path(tempfile.mkdtemp(prefix="obs-home-checkpoint-"))
        boards_root = home / ".local/share/capsule/boards"
        db_path = make_board_db(boards_root, checkpoint=True)
        wal_path = db_path.parent / "kanban.db-wal"
        self.assertTrue(not wal_path.exists() or wal_path.stat().st_size == 0)
        before = db_path.read_bytes()
        samples = obs_probe.collect_boards(home, now=NOW)
        self.assertEqual(before, db_path.read_bytes(), "read-only collection must not mutate the fixture db")

        got = self._assert_common_samples(samples)
        self.assertEqual(got[("capsule.board.db.bytes", (("board", "capsule-corp"),))], os.path.getsize(db_path))
        self.assertEqual(got[("capsule.board.wal.bytes", (("board", "capsule-corp"),))], 0.0)

    def test_multiple_boards_each_produce_their_own_samples(self):
        make_board_db(self.boards_root, board="second-board")
        samples = obs_probe.collect_boards(self.home, now=NOW)
        boards_seen = {s.labels.get("board") for s in samples if "board" in s.labels}
        self.assertEqual(boards_seen, {"capsule-corp", "second-board"})

    def test_one_bad_board_does_not_hide_the_others(self):
        """I2: a board whose db is missing `task_runs` (an older board, or one
        mid-migration) must skip only itself. The collector-level try/except
        in collect() used to drop every board's samples for one bad one."""
        broken_dir = self.boards_root / "legacy-board"
        broken_dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(broken_dir / "kanban.db"))
        con.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL,"
                          " consecutive_failures INTEGER NOT NULL DEFAULT 0);"
                          "INSERT INTO tasks VALUES ('t1', 'ready', 0);")
        con.commit()
        con.close()
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            samples = obs_probe.collect_boards(self.home, now=NOW)
        finally:
            sys.stderr = old_stderr
        boards_seen = {s.labels.get("board") for s in samples if "board" in s.labels}
        self.assertEqual(boards_seen, {"capsule-corp"}, "the healthy board must still emit")
        self._assert_common_samples(samples)
        self.assertIn("obs_probe: boards skipped legacy-board:", buf.getvalue())
        self.assertIn("task_runs", buf.getvalue())

    def test_missing_boards_dir_returns_no_samples(self):
        empty_home = Path(tempfile.mkdtemp(prefix="obs-empty-"))
        # No boards at all -> no samples, not a raise; a genuinely absent
        # directory must not explode the collector either.
        self.assertEqual(obs_probe.collect_boards(empty_home, now=NOW), [])


class Docker(unittest.TestCase):
    def test_collect_docker_exact_samples(self):
        run = full_fake_run()
        samples = obs_probe.collect_docker(run, now=NOW)
        got = {(s.name, s.labels.get("container")): s.value for s in samples}
        self.assertEqual(got[("capsule.container.cpu.pct", "capsule-corp-postgres")], 1.23)
        self.assertAlmostEqual(got[("capsule.container.mem.bytes", "capsule-corp-postgres")], 123.4 * 1024 ** 2)
        self.assertEqual(got[("capsule.container.cpu.pct", "capsule-corp-grafana")], 0.50)
        self.assertNotIn(("capsule.container.cpu.pct", "some-other-container"), got)
        self.assertEqual(got[("capsule.container.restarts", "capsule-corp-loki")], 1.0)
        self.assertEqual(got[("capsule.container.up", "capsule-corp-loki")], 1.0)
        self.assertEqual(got[("capsule.container.up", "capsule-corp-fluent-bit")], 0.0)
        for s in samples:
            self.assertIn(s.name, obs_probe.NAMES)

    def test_docker_stats_failure_raises(self):
        run = full_fake_run(docker_stats=(1, "", "docker: daemon not running"))
        with self.assertRaises(Exception):
            obs_probe.collect_docker(run, now=NOW)

    def test_one_absent_container_does_not_hide_the_others(self):
        """review Important 1: a single multi-name `docker inspect` call
        exits non-zero the instant ANY one of the seven names does not
        exist, which used to drop every docker sample wholesale. Inspecting
        one container at a time means an absent one reports only
        capsule.container.up=0 (no restarts) and leaves its six siblings
        untouched."""
        run = full_fake_run(inspect_overrides={
            "capsule-corp-loki": (1, "", "Error: No such object: capsule-corp-loki"),
        })
        samples = obs_probe.collect_docker(run, now=NOW)
        got = {(s.name, s.labels.get("container")): s.value for s in samples}
        self.assertEqual(got[("capsule.container.up", "capsule-corp-loki")], 0.0)
        self.assertNotIn(("capsule.container.restarts", "capsule-corp-loki"), got)
        # every other container's inspect-derived samples are unaffected
        self.assertEqual(got[("capsule.container.up", "capsule-corp-postgres")], 1.0)
        self.assertEqual(got[("capsule.container.restarts", "capsule-corp-postgres")], 0.0)
        self.assertEqual(got[("capsule.container.up", "capsule-corp-fluent-bit")], 0.0)
        self.assertEqual(got[("capsule.container.restarts", "capsule-corp-fluent-bit")], 0.0)
        self.assertEqual(got[("capsule.container.up", "capsule-corp-infisical-redis")], 1.0)
        # docker stats (a separate call) is entirely unaffected by the absent container
        self.assertEqual(got[("capsule.container.cpu.pct", "capsule-corp-postgres")], 1.23)
        for s in samples:
            self.assertIn(s.name, obs_probe.NAMES)


class Sandbox(unittest.TestCase):
    """row 127, M040: docs/EGRESS.md's egress-proof section is what this
    collector backs - a live network-attachment count per worker container."""

    def _run(self, ps=None, inspect_overrides=None):
        resp = {"docker_ps": ps or (0, DOCKER_PS_OUT, "")}
        for name, line in SANDBOX_INSPECT_LINES.items():
            resp[("docker_inspect", name)] = (0, line, "")
        for name, r in (inspect_overrides or {}).items():
            resp[("docker_inspect", name)] = r
        return FakeRun(resp)

    def test_collect_sandbox_exact_samples(self):
        samples = obs_probe.collect_sandbox(self._run(), now=NOW)
        got = {(s.name, s.labels.get("container")): s.value for s in samples}
        self.assertEqual(got[("capsule.sandbox.containers", None)], 2.0)
        run = self._run()
        obs_probe.collect_sandbox(run, now=NOW)
        ps = next(c[0] for c in run.calls if "ps" in c[0][:3])
        self.assertIn("label=capsule-run-id", ps)
        self.assertEqual(got[("capsule.sandbox.network.attached", "cap-dev-9f3a")], 0.0)
        self.assertEqual(got[("capsule.sandbox.network.attached", "cap-qa-1b2c")], 1.0)
        for s in samples:
            self.assertIn(s.name, obs_probe.NAMES)

    def test_docker_ps_failure_raises(self):
        run = self._run(ps=(1, "", "docker: daemon not running"))
        with self.assertRaises(Exception):
            obs_probe.collect_sandbox(run, now=NOW)

    def test_uninspectable_container_reports_attached_not_safe(self):
        """review: unknown is not "safe" - a container docker inspect can't
        reach still counts as network-attached, the opposite default from
        collect_docker's up=0 (there is nothing safe to assume about egress)."""
        run = self._run(inspect_overrides={
            "cap-qa-1b2c": (1, "", "Error: No such object: cap-qa-1b2c"),
        })
        samples = obs_probe.collect_sandbox(run, now=NOW)
        got = {(s.name, s.labels.get("container")): s.value for s in samples}
        self.assertEqual(got[("capsule.sandbox.network.attached", "cap-qa-1b2c")], 1.0)
        # the other container's sample is unaffected
        self.assertEqual(got[("capsule.sandbox.network.attached", "cap-dev-9f3a")], 0.0)


class Launchd(unittest.TestCase):
    def setUp(self):
        self.state_dir = Path(tempfile.mkdtemp(prefix="obs-state-"))
        (self.state_dir / "jobs").mkdir(parents=True)

    def _write_stamp(self, job, **fields):
        (self.state_dir / "jobs" / f"{job}.json").write_text(json.dumps(fields))

    def test_collect_launchd_loaded_and_stamps(self):
        self._write_stamp("heal", start="2026-09-08T11:00:00+00:00",
                           end="2026-09-08T11:59:00+00:00", duration_s=30, exit=0,
                           skipped=None, commit=None, log="/x")
        run = full_fake_run()
        samples = obs_probe.collect_launchd(run, self.state_dir, now=NOW, prefix="dev.capsule")
        got = {(s.name, tuple(sorted(s.labels.items()))): s.value for s in samples}
        heal = ("capsule.job.loaded", (("label", "dev.capsule.capsule-corp-heal"),))
        self.assertEqual(got[heal], 1.0)
        cleanup = ("capsule.job.loaded", (("label", "dev.capsule.capsule-corp-cleanup"),))
        self.assertEqual(got[cleanup], 0.0)
        self.assertEqual(got[("capsule.job.last_exit", (("job", "heal"),))], 0.0)
        self.assertEqual(got[("capsule.job.last_duration_s", (("job", "heal"),))], 30.0)
        # end was one minute before NOW
        self.assertAlmostEqual(got[("capsule.job.age_s", (("job", "heal"),))], 60.0)
        for s in samples:
            self.assertIn(s.name, obs_probe.NAMES)

    def test_launchctl_failure_raises(self):
        run = full_fake_run(launchd=(1, "", "launchctl: not found"))
        with self.assertRaises(Exception):
            obs_probe.collect_launchd(run, self.state_dir, now=NOW)


class Bridge(unittest.TestCase):
    def setUp(self):
        self.state_dir = Path(tempfile.mkdtemp(prefix="obs-state-"))
        (self.state_dir / "jobs").mkdir(parents=True)
        self.logs_dir = Path(tempfile.mkdtemp(prefix="obs-logs-"))

    def test_collect_bridge_exact_samples(self):
        (self.state_dir / "jobs" / "sprint-sync.json").write_text(json.dumps({
            "job": "sprint-sync", "start": "2026-09-08T11:58:00+00:00",
            "end": "2026-09-08T11:59:30+00:00", "duration_s": 90, "exit": 0,
            "skipped": None, "commit": None, "log": "/x",
        }))
        old_log = self.logs_dir / "sprint-sync-2026-09-08T110000.log"
        old_log.write_text("ledger pushed: 2 entries\n")
        new_log = self.logs_dir / "sprint-sync-2026-09-08T115900.log"
        new_log.write_text("some noise\nledger pushed: 5 entries\nmore noise\n")
        os.utime(old_log, (1, 1))
        os.utime(new_log, (2, 2))
        samples = obs_probe.collect_bridge(self.state_dir, self.logs_dir, now=NOW)
        got = {s.name: s.value for s in samples}
        self.assertAlmostEqual(got["capsule.bridge.tick.age_s"], 30.0)
        self.assertEqual(got["capsule.bridge.tick.exit"], 0.0)
        self.assertEqual(got["capsule.bridge.ledger.pushed"], 5.0)  # from the newest log, not the old one
        for s in samples:
            self.assertIn(s.name, obs_probe.NAMES)

    def test_missing_stamp_raises(self):
        with self.assertRaises(Exception):
            obs_probe.collect_bridge(self.state_dir, self.logs_dir, now=NOW)


class Heal(unittest.TestCase):
    def test_collect_heal_exact_samples(self):
        state_dir = Path(tempfile.mkdtemp(prefix="obs-state-"))
        (state_dir / "heal.json").write_text(json.dumps({
            "docker": {"fails": 0, "first_fail": None},
            "infisical": {"fails": 3, "first_fail": "2026-09-08T10:00:00+00:00"},
        }))
        samples = obs_probe.collect_heal(state_dir, now=NOW)
        got = {s.labels["component"]: s.value for s in samples}
        self.assertEqual(got["docker"], 0.0)
        self.assertEqual(got["infisical"], 3.0)
        for s in samples:
            self.assertEqual(s.name, "capsule.heal.fails")

    def test_missing_heal_json_raises(self):
        state_dir = Path(tempfile.mkdtemp(prefix="obs-state-"))
        with self.assertRaises(Exception):
            obs_probe.collect_heal(state_dir, now=NOW)


class Host(unittest.TestCase):
    def test_collect_host_exact_samples(self):
        run = full_fake_run()
        samples = obs_probe.collect_host(run, Path.home(), now=NOW)
        got = {s.name: s.value for s in samples}
        self.assertEqual(got["capsule.host.disk.pct"], 79.0)
        self.assertEqual(got["capsule.host.load1"], 2.79)

    def test_df_failure_raises(self):
        run = full_fake_run(df=(1, "", "df: no such file"))
        with self.assertRaises(Exception):
            obs_probe.collect_host(run, Path.home(), now=NOW)


class CollectWrapper(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="obs-home-"))
        boards = self.home / ".local/share/capsule/boards"
        db_path = make_board_db(boards, checkpoint=True)
        add_card_rows(db_path, int(NOW.timestamp()))
        self.state_dir = Path(tempfile.mkdtemp(prefix="obs-state-"))
        (self.state_dir / "jobs").mkdir(parents=True)
        (self.state_dir / "jobs" / "sprint-sync.json").write_text(json.dumps({
            "end": "2026-09-08T11:59:30+00:00", "exit": 0,
        }))
        (self.state_dir / "jobs" / "heal.json").write_text(json.dumps({
            "end": "2026-09-08T11:59:00+00:00", "duration_s": 30, "exit": 0,
        }))
        (self.state_dir / "heal.json").write_text(json.dumps({"docker": {"fails": 0, "first_fail": None}}))
        self.logs_dir = Path(tempfile.mkdtemp(prefix="obs-logs-"))
        (self.logs_dir / "sprint-sync-1.log").write_text("ledger pushed: 1 entries\n")

    def test_one_failing_collector_does_not_stop_the_others(self):
        run = full_fake_run(docker_stats=(1, "", "docker: daemon not running"))
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            status = []
            samples = obs_probe.collect(run, self.home, self.state_dir, self.logs_dir, now=NOW,
                                        status=status)
        finally:
            sys.stderr = old_stderr
        names = {s.name for s in samples}
        self.assertIn("capsule.postgres.db.bytes", names)     # postgres still ran
        self.assertIn("capsule.board.cards", names)            # boards still ran
        self.assertNotIn("capsule.container.cpu.pct", names)   # docker did not
        self.assertIn("obs_probe: docker skipped:", buf.getvalue())
        self.assertEqual(dict(status)["docker"], False)
        self.assertEqual(dict(status)["postgres"], True)

    def test_every_sample_name_is_a_names_key_and_every_names_key_is_emitted(self):
        run = full_fake_run()
        samples = obs_probe.collect(run, self.home, self.state_dir, self.logs_dir, now=NOW)
        emitted = {s.name for s in samples}
        self.assertTrue(emitted.issubset(obs_probe.NAMES.keys()))
        self.assertEqual(emitted, set(obs_probe.NAMES.keys()),
                          f"missing: {set(obs_probe.NAMES) - emitted}, extra: {emitted - set(obs_probe.NAMES)}")


class Cards(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="obs-cards-"))
        self.db = make_board_db(self.home / ".local/share/capsule/boards", checkpoint=True)
        add_card_rows(self.db, int(NOW.timestamp()))

    def _got(self):
        samples = obs_probe.collect_cards(self.home, now=NOW)
        for s in samples:
            self.assertIn(s.name, obs_probe.NAMES)
            self.assertEqual(s.service, "capsule")
        return {(s.name, tuple(sorted(s.labels.items()))): s.value for s in samples}

    def test_per_card_rollups(self):
        got = self._got()
        t1 = (("board", "capsule-corp"), ("task_id", "t1"))
        t6 = (("board", "capsule-corp"), ("task_id", "t6"))
        self.assertEqual(got[("capsule.card.runs", t1)], 4.0)   # 3 here + t1's fixture run
        self.assertEqual(got[("capsule.card.review_rounds", t1)], 2.0)
        self.assertEqual(got[("capsule.card.errors", t1)], 1.0)
        self.assertEqual(got[("capsule.card.error_repeats", t1)], 1.0)
        self.assertEqual(got[("capsule.card.errors", t6)], 2.0)
        self.assertEqual(got[("capsule.card.error_repeats", t6)], 2.0,
                          "two failures differing only in a pid and a hex path "
                          "are the same failure twice")
        self.assertEqual(got[("capsule.card.duration_s", t6)], 20.0)

    def test_the_task_id_label_is_capped_at_the_two_hundred_newest_runs(self):
        con = sqlite3.connect(str(self.db))
        con.executemany(
            "INSERT INTO task_runs (task_id, profile, status, started_at,"
            " ended_at, outcome) VALUES (?,?,?,?,?,?)",
            [(f"t_pad{i}", "dev", "done", int(NOW.timestamp()) - 10,
              int(NOW.timestamp()), "completed") for i in range(250)])
        con.commit(); con.close()
        cards = {s.labels["task_id"] for s in obs_probe.collect_cards(self.home, now=NOW)
                 if "task_id" in s.labels}
        self.assertLessEqual(len(cards), obs_probe.CARD_RUN_CAP)
        self.assertNotIn("t1", cards, "the 250 newer runs push t1 out of the window")

    def test_a_board_without_task_events_still_reports_run_samples(self):
        con = sqlite3.connect(str(self.db))
        con.execute("DROP TABLE task_events")
        con.commit()
        con.close()
        got = self._got()
        t1 = (("board", "capsule-corp"), ("task_id", "t1"))
        self.assertEqual(got[("capsule.card.runs", t1)], 4.0)
        self.assertEqual(got[("capsule.card.review_rounds", t1)], 0.0)

    def test_the_retired_session_samples_are_not_emitted(self):
        retired = {"capsule.card.tokens.input", "capsule.card.tokens.output",
                   "capsule.card.tokens.cache_read", "capsule.card.tokens.cache_write",
                   "capsule.card.tokens.reasoning", "capsule.card.turns",
                   "capsule.card.api_calls", "capsule.card.tool_calls",
                   "capsule.card.cost_usd", "capsule.card.runs.unjoined"}
        self.assertFalse(retired & set(obs_probe.NAMES))
        emitted = {s.name for s in obs_probe.collect_cards(self.home, now=NOW)}
        self.assertFalse(retired & emitted)

    def test_one_bad_board_does_not_stop_the_others(self):
        boards = self.home / ".local/share/capsule/boards"
        make_board_db(boards, board="sample-app", checkpoint=True).write_bytes(b"not a database")
        buf, old = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            names = {s.labels.get("board") for s in obs_probe.collect_cards(self.home, now=NOW)}
        finally:
            sys.stderr = old
        self.assertIn("capsule-corp", names)
        self.assertIn("obs_probe: cards skipped sample-app:", buf.getvalue())


class Power(unittest.TestCase):
    def test_delegation_counts(self):
        run = full_fake_run()
        got = {(s.name, tuple(sorted(s.labels.items()))): s.value
               for s in obs_probe.collect_power(run, now=NOW)}
        self.assertEqual(got[("capsule.delegation.rulings.7d", ())], 68.0)
        self.assertEqual(got[("capsule.delegation.decisions.7d", ())], 10.0)

    def test_a_retrieval_push_row_becomes_a_tokens_retrieval_sample(self):
        """Row 201 (A1). board_bridge never writes to `samples` directly - the
        `retrieval_push` event it logs at dispatch is replayed here into
        `capsule.card.retrieval.tokens` - fix wave 1 (review M1): deliberately
        OUTSIDE the `capsule.card.tokens.%` family report.sh's power line
        sums, so the block's already-counted prompt bytes are never summed a
        second time into the existing token median. report.sh gives this
        series its own median line instead (see test_report_power.sh)."""
        run = full_fake_run()
        got = {(s.name, tuple(sorted(s.labels.items()))): s.value
               for s in obs_probe.collect_power(run, now=NOW)}
        self.assertEqual(got[("capsule.card.retrieval.tokens",
                              (("board", "capsule-corp"), ("task_id", "t_63a8b207")))], 1600.0)

    def test_psql_failure_raises_so_the_collector_is_skipped(self):
        run = full_fake_run(power=(1, "", "ERROR: connection refused"))
        with self.assertRaises(Exception):
            obs_probe.collect_power(run, now=NOW)

    def test_the_ruling_set_excludes_the_events_that_ask_the_owner(self):
        for asked in ("'pr_ready'", "'stuck'", "'decision_open'"):
            self.assertNotIn(asked, obs_probe.POWER_SQL.split("union all")[0],
                             f"{asked} asks the owner; it is not a machine ruling")
        for his in ("'decision_answered'", "'approved'", "'gate_changes'"):
            self.assertIn(his, obs_probe.POWER_SQL)

    def test_the_ruling_set_drops_events_whose_writers_were_deleted(self):
        clause = obs_probe.POWER_SQL.split("union all")[0]
        for gone in ("escalated", "spec_ready"):
            self.assertNotIn(f"'{gone}'", clause)

    def test_the_ruling_set_names_no_event_nothing_ever_writes(self):
        """`review_requested` is a LEDGER kind (ledger.KINDS) and a legacy
        `task_events.kind`; no writer anywhere puts it in a `kind='card'`
        event's `payload.event`, so counting it inflated nothing and just made
        the clause lie about what it measures (fix round 2). Every other name
        in the clause has a producer - proven by grepping the writers, not by
        restating the list."""
        clause = obs_probe.POWER_SQL.split("union all")[0]
        self.assertNotIn("'review_requested'", clause)
        # quality_gate was the only writer of these three
        for retired in ("quality_pass", "quality_fail", "repair_needed"):
            self.assertNotIn(retired, clause)
        for gone in ("repair_hold", "repair_release", "repair_resolved",
                     "gate_pr_ready", "gate_not_merged"):
            self.assertNotIn(gone, clause)
        maint = Path(__file__).resolve().parents[1]
        sources = "\n".join(p.read_text(encoding="utf-8")
                            for p in sorted(maint.glob("*.py"))
                            if p.name != "obs_probe.py")
        names = re.findall(r"'([a-z_]+)'", clause.split(" in (", 1)[1])
        self.assertGreaterEqual(len(names), 6, "ruling set unexpectedly short")
        for name in names:
            self.assertIn(f'"{name}"', sources,
                          f"{name!r} appears in POWER_SQL's ruling set but no "
                          "writer under maintenance/ ever produces it")


class Fingerprint(unittest.TestCase):
    def test_pids_epochs_and_hexes_collapse(self):
        a = obs_probe.fingerprint("worker pid 4711 died reading /tmp/ab39f01c2d/x")
        b = obs_probe.fingerprint("worker pid 98765 died reading /tmp/0011223344/x")
        self.assertEqual(a, b)

    def test_different_failures_stay_different(self):
        self.assertNotEqual(obs_probe.fingerprint("timed out after 900s"),
                             obs_probe.fingerprint("HTTP 429 rate_limit_error"))

    def test_empty_is_stable_and_short(self):
        self.assertEqual(len(obs_probe.fingerprint("")), 8)


class ParseMemBytes(unittest.TestCase):
    def test_units_table(self):
        cases = [
            ("123.4MiB / 7.6GiB", 123.4 * 1024 ** 2),
            ("1.0GiB / 7.6GiB", 1.0 * 1024 ** 3),
            ("512KiB / 7.6GiB", 512 * 1024),
            ("100B / 7.6GiB", 100.0),
            ("0B / 0B", 0.0),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertAlmostEqual(obs_probe._parse_mem_bytes(raw), expected)

    def test_garbage_and_none_are_zero(self):
        self.assertEqual(obs_probe._parse_mem_bytes("not a size"), 0.0)
        self.assertEqual(obs_probe._parse_mem_bytes(None), 0.0)
        self.assertEqual(obs_probe._parse_mem_bytes(""), 0.0)


class ToCsv(unittest.TestCase):
    def test_quotes_labels_json_correctly(self):
        s = obs_probe.Sample(ts="2026-09-08T12:00:00+00:00", service="capsule",
                              name="capsule.board.cards", value=2.0, labels={"board": "capsule-corp", "status": "ready"})
        text = obs_probe.to_csv([s])
        rows = list(__import__("csv").reader(io.StringIO(text)))
        self.assertEqual(len(rows), 1)
        ts, service, name, value, labels_json = rows[0]
        self.assertEqual(ts, s.ts)
        self.assertEqual(service, "capsule")
        self.assertEqual(name, "capsule.board.cards")
        self.assertEqual(value, "2.0")
        self.assertEqual(json.loads(labels_json), {"board": "capsule-corp", "status": "ready"})
        self.assertEqual(labels_json, json.dumps({"board": "capsule-corp", "status": "ready"}, sort_keys=True))

    def test_empty_samples_is_empty_csv(self):
        self.assertEqual(obs_probe.to_csv([]), "")


class Ship(unittest.TestCase):
    def test_argv_shape_and_stdin(self):
        run = FakeRun(ship=(0, "", ""), rollup=(0, "", ""))
        ok = obs_probe.ship("ts,service,name,value,labels\n", run)
        self.assertTrue(ok)
        self.assertEqual(len(run.calls), 2)
        argv, kwargs = run.calls[0]
        self.assertEqual(argv, ["docker", "exec", "-i", "capsule-corp-postgres", "psql", "-U", "ops",
                                 "-d", "ops", "-v", "ON_ERROR_STOP=1", "-c",
                                 "COPY samples (ts, service, name, value, labels) FROM STDIN WITH (FORMAT csv)"])
        self.assertEqual(kwargs["input"], "ts,service,name,value,labels\n")
        self.assertTrue(kwargs.get("text"))

    def test_ship_then_rollup_upserts_last_two_hours_in_one_statement(self):
        run = FakeRun(ship=(0, "", ""), rollup=(0, "", ""))
        self.assertTrue(obs_probe.ship("x\n", run))
        self.assertEqual([FakeRun._key(a) for a, _ in run.calls], ["ship", "rollup"])
        argv, _ = run.calls[1]
        sql = argv[argv.index("-c") + 1]
        self.assertIn("INSERT INTO samples_hourly", sql)
        self.assertIn("date_trunc('hour', now()) - interval '1 hour'", sql)
        self.assertIn("ON CONFLICT (service, name, bucket) DO UPDATE", sql)
        self.assertEqual(sql.count(";"), 0, "one statement, so one transaction")

    def test_rollup_failure_does_not_fail_the_ship(self):
        for bad in ((1, "", "ERROR: relation does not exist"), RuntimeError("boom")):
            self.assertTrue(obs_probe.ship("x\n", FakeRun(ship=(0, "", ""), rollup=bad)))

    def test_psql_failure_returns_not_ok(self):
        run = FakeRun(ship=(1, "", "ERROR:  duplicate key value violates unique constraint"))
        res = obs_probe.ship("x\n", run)
        self.assertFalse(res.ok)
        self.assertIsNone(res.skip_reason, "a real COPY error is not a shut door")

    def test_exception_returns_not_ok(self):
        run = FakeRun(ship=RuntimeError("boom"))
        res = obs_probe.ship("x\n", run)
        self.assertFalse(res.ok)
        self.assertIsNone(res.skip_reason)

    def test_shut_door_stderrs_are_classified_as_skips(self):
        for err in ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
                    "Error response from daemon: No such container: capsule-corp-postgres",
                    "psql: error: connection to server failed: Connection refused",
                    'ERROR:  relation "samples" does not exist'):
            with self.subTest(err=err):
                res = obs_probe.ship("x\n", FakeRun(ship=(1, "", err)))
                self.assertFalse(res.ok)
                self.assertTrue(res.skip_reason, f"{err!r} should read as a shut door")

    def test_missing_docker_binary_is_a_skip(self):
        res = obs_probe.ship("x\n", FakeRun(ship=FileNotFoundError("no such file: docker")))
        self.assertFalse(res.ok)
        self.assertIn("docker not available", res.skip_reason)


class Main(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="obs-home-"))
        make_board_db(self.home / ".local/share/capsule/boards")
        self.state_dir = Path(tempfile.mkdtemp(prefix="obs-state-"))
        (self.state_dir / "jobs").mkdir(parents=True)
        (self.state_dir / "jobs" / "sprint-sync.json").write_text(json.dumps(
            {"end": "2026-09-08T11:59:30+00:00", "exit": 0}))
        self.enterContext(unittest.mock.patch.dict(
            os.environ, CAPSULE_LAUNCHD_PREFIX="dev.capsule"))
        (self.state_dir / "heal.json").write_text(json.dumps({"docker": {"fails": 0, "first_fail": None}}))
        self.logs_dir = Path(tempfile.mkdtemp(prefix="obs-logs-"))
        (self.logs_dir / "sprint-sync-1.log").write_text("ledger pushed: 1 entries\n")

    def test_exit_0_with_partial_failure_and_summary_line(self):
        run = full_fake_run(docker_stats=(1, "", "docker: daemon not running"))
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            rc = obs_probe.main(run=run, home=self.home, state_dir=self.state_dir,
                                 logs_dir=self.logs_dir, now=NOW)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertRegex(out, r"obs_probe: \d+ samples, 9/10 collectors ok")
        # never a raw value on stdout
        self.assertNotIn("123456", out)

    def test_exit_1_when_every_collector_fails(self):
        boom = RuntimeError("boom")
        run = FakeRun(postgres=boom, docker_stats=boom, launchd=boom, df=boom)
        home = Path(tempfile.mkdtemp(prefix="obs-empty-home-"))
        state_dir = Path(tempfile.mkdtemp(prefix="obs-empty-state-"))  # no jobs/, no heal.json -> bridge/heal fail
        (state_dir / "jobs").mkdir()
        logs_dir = Path(tempfile.mkdtemp(prefix="obs-empty-logs-"))
        buf = io.StringIO()
        old_stderr, old_stdout = sys.stderr, sys.stdout
        sys.stderr = buf
        sys.stdout = io.StringIO()   # the 0/7 summary line belongs in the test, not the suite log
        # Since I2 the boards collector isolates each board, so a corrupt
        # kanban.db can no longer fail the collector itself (it skips that one
        # board and returns the rest, which counts as an ok, empty collector).
        # Stub it out to model the only remaining collector-level boards
        # failure and keep the "nothing worked at all" path covered.
        # collect_cards isolates each board exactly like collect_boards (same
        # I2 rule - see collect_cards' docstring); with no boards directory at
        # all it would otherwise trivially succeed with zero samples, which
        # would falsely keep ship() reachable in this "nothing worked" test.
        real_boards = obs_probe.collect_boards
        real_cards = obs_probe.collect_cards
        obs_probe.collect_boards = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        obs_probe.collect_cards = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            rc = obs_probe.main(run=run, home=home, state_dir=state_dir, logs_dir=logs_dir, now=NOW)
        finally:
            obs_probe.collect_boards = real_boards
            obs_probe.collect_cards = real_cards
            sys.stderr, sys.stdout = old_stderr, old_stdout
        self.assertEqual(rc, 1)
        self.assertEqual(buf.getvalue().count("obs_probe:"), 10, "one skip line per failed collector")
        self.assertNotIn("ship", buf.getvalue(), "nothing to ship when no collector worked")

    def _main_capturing(self, run):
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = obs_probe.main(run=run, home=self.home, state_dir=self.state_dir,
                                 logs_dir=self.logs_dir, now=NOW)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return rc, out.getvalue(), err.getvalue()

    def test_successful_ship_is_followed_by_the_rollup(self):
        run = full_fake_run()
        rc, _, _ = self._main_capturing(run)
        self.assertEqual(rc, 0)
        keys = [FakeRun._key(a) for a, _ in run.calls]
        self.assertIn("rollup", keys)
        self.assertLess(keys.index("ship"), keys.index("rollup"))

    def test_failed_ship_skips_the_rollup(self):
        run = full_fake_run(ship=(1, "", "ERROR:  invalid input syntax for type numeric"))
        self._main_capturing(run)
        self.assertNotIn("rollup", [FakeRun._key(a) for a, _ in run.calls])

    def test_ship_failure_is_exit_1(self):
        # A real COPY error (not a shut door): still exit 1, still no skip line.
        rc, out, _ = self._main_capturing(
            full_fake_run(ship=(1, "", "ERROR:  invalid input syntax for type numeric")))
        self.assertEqual(rc, 1)
        self.assertNotIn("ship skipped", out)

    def test_shut_door_ship_is_exit_0_with_a_skip_line(self):
        for err in ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
                    "psql: error: connection to server at \"127.0.0.1\" failed: Connection refused",
                    'ERROR:  relation "samples" does not exist'):
            with self.subTest(err=err):
                rc, out, _ = self._main_capturing(full_fake_run(ship=(1, "", err)))
                self.assertEqual(rc, 0, f"{err!r} must not fail the job")
                self.assertIn("obs_probe: ship skipped:", out)

    def test_missing_docker_binary_ship_is_exit_0(self):
        rc, out, _ = self._main_capturing(
            full_fake_run(ship=FileNotFoundError("[Errno 2] No such file or directory: 'docker'")))
        self.assertEqual(rc, 0)
        self.assertIn("obs_probe: ship skipped:", out)


if __name__ == "__main__":
    unittest.main()
